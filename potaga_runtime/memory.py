"""Memory stores — Phase 4.

The seven partitioned stores behind a swappable backend protocol:

- `FilesystemBackend` (default, GA): local files with optimistic concurrency
  (SHA-256 preconditions, spec §9.4 / policy §B.3), immutable version history,
  and provenance metadata on every write (spec §9.5).
- `ClaudeMemoryStoresBackend`: a thin mapping onto the memory-store session
  API shape described in spec §9.3 (stores attached via resources[] at session
  creation, hash-preconditioned writes). The concrete client is injected by
  the operator — this module does not hardcode claims about the availability
  or exact surface of any beta API; verify at https://docs.claude.com before
  wiring it, and fall back to the filesystem backend otherwise.

Access grants remain fixed at session creation and are enforced on every
write, with path-escape protection. `attachments()` renders an agent's
mounts in the resources[] shape so the session opening can state exactly
what is mounted with which access.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Tuple

STORES = ("shared", "code", "tests", "reviews", "docs", "research", "cache")

WRITE_GRANTS: Dict[str, set] = {
    "architect": {"shared", "cache"},
    "coder": {"code", "cache"},
    "tester": {"tests", "cache"},
    "reviewer": {"reviews", "cache"},
    "docs": {"docs", "shared", "cache"},
    "research": {"research", "shared", "cache"},
}

STORE_INSTRUCTIONS = {
    "shared": "MULTI_AGENT_PLAN.md and ADRs. Read every turn.",
    "code": "Source code, configs, dependencies.md.",
    "tests": "Test results, coverage, failure analysis.",
    "reviews": "Review findings, OWASP checklists, verdicts.",
    "docs": "README, API docs, changelogs.",
    "research": "Findings with URL + retrieval date.",
    "cache": "Scratch space. Write under your own /cache/<agent>/ prefix.",
}

OCC_MAX_RETRIES = 5  # policy §B.3


class AccessDenied(PermissionError):
    pass


class PreconditionFailed(RuntimeError):
    """SHA-256 precondition mismatch: another writer got there first."""


class OCCExhausted(RuntimeError):
    """Optimistic-concurrency retries exhausted — escalate to the Orchestrator."""


# --------------------------------------------------------------- backend
class MemoryBackend(Protocol):
    def read(self, store: str, relpath: str) -> str: ...
    def read_versioned(self, store: str, relpath: str) -> Tuple[str, str]: ...
    def write(self, store: str, relpath: str, content: str, provenance: Dict,
              expected_hash: Optional[str] = None) -> str: ...
    def exists(self, store: str, relpath: str) -> bool: ...
    def history(self, store: str, relpath: str) -> List[Dict]: ...
    def root_of(self, store: str) -> pathlib.Path: ...


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class FilesystemBackend:
    """Local-disk implementation. Every write creates an immutable version
    copy plus a provenance record under <store>/.versions/, giving the
    complete audit trail of spec §9.5."""

    def __init__(self, workspace: pathlib.Path) -> None:
        self.workspace = workspace
        for s in STORES:
            (workspace / f"potaga-{s}").mkdir(parents=True, exist_ok=True)

    def root_of(self, store: str) -> pathlib.Path:
        if store not in STORES:
            raise KeyError(f"unknown store '{store}'")
        return self.workspace / f"potaga-{store}"

    def _target(self, store: str, relpath: str) -> pathlib.Path:
        root = self.root_of(store).resolve()
        target = (root / relpath).resolve()
        if root not in target.parents and target != root:
            raise AccessDenied(f"path escapes store: {relpath}")
        return target

    def exists(self, store: str, relpath: str) -> bool:
        return self._target(store, relpath).exists()

    def read(self, store: str, relpath: str) -> str:
        return self._target(store, relpath).read_text()

    def read_versioned(self, store: str, relpath: str) -> Tuple[str, str]:
        content = self.read(store, relpath)
        return content, _sha(content)

    def write(self, store: str, relpath: str, content: str, provenance: Dict,
              expected_hash: Optional[str] = None) -> str:
        target = self._target(store, relpath)
        if expected_hash is not None:
            current = _sha(target.read_text()) if target.exists() else None
            if current != expected_hash:
                raise PreconditionFailed(
                    f"{store}/{relpath}: expected {str(expected_hash)[:12]}…, "
                    f"found {str(current)[:12]}…")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        new_hash = _sha(content)
        # immutable version + provenance
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
        vdir = self.root_of(store) / ".versions" / relpath
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{ts}.content").write_text(content)
        (vdir / f"{ts}.meta.json").write_text(json.dumps(
            {**provenance, "sha256": new_hash,
             "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}))
        return new_hash

    def history(self, store: str, relpath: str) -> List[Dict]:
        vdir = self.root_of(store) / ".versions" / relpath
        if not vdir.exists():
            return []
        return [json.loads(f.read_text()) for f in sorted(vdir.glob("*.meta.json"))]


class ClaudeMemoryStoresBackend:
    """Adapter onto a memory-stores client (spec §9.3 shape). The operator
    injects `client`, an object exposing:

        client.read(store_id, path) -> {"content": str, "sha256": str}
        client.write(store_id, path, content, metadata, expected_sha256=None)
        client.history(store_id, path) -> list[dict]

    Store IDs come from `store_ids` (name -> id). This class makes no claims
    about any specific beta's availability — confirm the current API at
    https://docs.claude.com and shape your client accordingly.
    """

    def __init__(self, client, store_ids: Dict[str, str], local_mirror: pathlib.Path) -> None:
        self._client = client
        self._ids = store_ids
        self._mirror = FilesystemBackend(local_mirror)  # local sandbox mount (L1 cache)

    def root_of(self, store: str) -> pathlib.Path:
        return self._mirror.root_of(store)

    def exists(self, store: str, relpath: str) -> bool:
        try:
            self._client.read(self._ids[store], relpath)
            return True
        except Exception:
            return False

    def read(self, store: str, relpath: str) -> str:
        return self._client.read(self._ids[store], relpath)["content"]

    def read_versioned(self, store: str, relpath: str) -> Tuple[str, str]:
        r = self._client.read(self._ids[store], relpath)
        return r["content"], r["sha256"]

    def write(self, store: str, relpath: str, content: str, provenance: Dict,
              expected_hash: Optional[str] = None) -> str:
        self._client.write(self._ids[store], relpath, content, provenance,
                           expected_sha256=expected_hash)
        self._mirror.write(store, relpath, content, provenance)  # keep L1 mirror warm
        return _sha(content)

    def history(self, store: str, relpath: str) -> List[Dict]:
        return self._client.history(self._ids[store], relpath)


# ---------------------------------------------------------------- facade
@dataclass(frozen=True)
class StoreGrant:
    agent: str

    def can_write(self, store: str) -> bool:
        return store in WRITE_GRANTS.get(self.agent, set())


class MemoryStores:
    """Grant-enforcing facade over a backend. Public API is stable across
    Phase 1 → 4; OCC and history are additive."""

    def __init__(self, workspace: pathlib.Path, backend: MemoryBackend | None = None) -> None:
        self.backend: MemoryBackend = backend or FilesystemBackend(workspace)

    def path(self, store: str) -> pathlib.Path:
        return self.backend.root_of(store)

    def grant(self, agent: str) -> StoreGrant:
        return StoreGrant(agent=agent)

    def attachments(self, agent: str) -> List[Dict]:
        """The agent's mounts in the resources[] shape of spec §9.3."""
        out = []
        for store in STORES:
            access = "read_write" if store in WRITE_GRANTS.get(agent, set()) else "read_only"
            out.append({"type": "memory_store", "store": f"potaga-{store}",
                        "access": access, "instructions": STORE_INSTRUCTIONS[store]})
        return out

    # ---------- agent-facing operations ----------
    def write(self, grant: StoreGrant, store: str, relpath: str, content: str,
              *, model: str, session_id: str,
              expected_hash: Optional[str] = None) -> pathlib.Path:
        if not grant.can_write(store):
            raise AccessDenied(f"agent '{grant.agent}' has no write grant on potaga-{store}")
        if store == "cache":
            relpath = f"cache/{grant.agent}/{relpath.lstrip('/')}"
        provenance = {"writer": grant.agent, "model": model, "session": session_id}
        self.backend.write(store, relpath, content, provenance, expected_hash=expected_hash)
        return self.backend.root_of(store) / relpath

    def read(self, store: str, relpath: str) -> str:
        return self.backend.read(store, relpath)

    def exists(self, store: str, relpath: str) -> bool:
        return self.backend.exists(store, relpath)

    def history(self, store: str, relpath: str) -> List[Dict]:
        return self.backend.history(store, relpath)

    # ---------- optimistic concurrency (policy §B.3) ----------
    def occ_update(self, grant: StoreGrant, store: str, relpath: str,
                   update: Callable[[str], str], *, model: str, session_id: str,
                   max_retries: int = OCC_MAX_RETRIES) -> str:
        """Read-modify-write with SHA-256 preconditions; retries up to
        `max_retries` on conflict, then raises OCCExhausted for the
        Orchestrator to escalate."""
        if store == "cache":
            raise ValueError("cache partitions are per-agent; OCC is for shared stores")
        for _ in range(max_retries):
            if self.backend.exists(store, relpath):
                current, expected = self.backend.read_versioned(store, relpath)
            else:
                current, expected = "", None
            try:
                self.write(grant, store, relpath, update(current),
                           model=model, session_id=session_id,
                           expected_hash=expected)
                return relpath
            except PreconditionFailed:
                continue
        raise OCCExhausted(f"{store}/{relpath}: {max_retries} OCC retries exhausted")
