"""Memory stores — Phase-1 filesystem implementation.

Seven partitions under the project workspace, mirroring the store layout in
SYSTEM_OVERVIEW.md. Access grants are fixed at session creation (like the
real memory-store attachment) and enforced on every write. Every write
carries provenance metadata. Swappable for the Claude Memory Stores backend
in Phase 4 without touching callers.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
from dataclasses import dataclass
from typing import Dict, Literal

STORES = ("shared", "code", "tests", "reviews", "docs", "research", "cache")

# write access per agent, per SYSTEM_OVERVIEW / prompt Tools sections.
# (plan merges in `shared` are Orchestrator-only and bypass agent grants.)
WRITE_GRANTS: Dict[str, set] = {
    "architect": {"shared", "cache"},
    "coder": {"code", "cache"},
    "tester": {"tests", "cache"},
    "reviewer": {"reviews", "cache"},
    "docs": {"docs", "shared", "cache"},
    "research": {"research", "shared", "cache"},
}


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class StoreGrant:
    agent: str
    root: pathlib.Path

    def can_write(self, store: str) -> bool:
        return store in WRITE_GRANTS.get(self.agent, set())


class MemoryStores:
    def __init__(self, workspace: pathlib.Path) -> None:
        self.root = workspace
        for s in STORES:
            (workspace / f"potaga-{s}").mkdir(parents=True, exist_ok=True)

    def path(self, store: str) -> pathlib.Path:
        if store not in STORES:
            raise KeyError(f"unknown store '{store}'")
        return self.root / f"potaga-{store}"

    def grant(self, agent: str) -> StoreGrant:
        return StoreGrant(agent=agent, root=self.root)

    # ---------- agent-facing operations ----------
    def write(self, grant: StoreGrant, store: str, relpath: str, content: str,
              *, model: str, session_id: str) -> pathlib.Path:
        if not grant.can_write(store):
            raise AccessDenied(f"agent '{grant.agent}' has no write grant on potaga-{store}")
        if store == "cache":
            relpath = f"cache/{grant.agent}/{relpath.lstrip('/')}"
        target = (self.path(store) / relpath).resolve()
        if self.path(store).resolve() not in target.parents and target != self.path(store).resolve():
            raise AccessDenied(f"path escapes store: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        provenance = {
            "writer": grant.agent, "model": model, "session": session_id,
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }
        target.with_suffix(target.suffix + ".prov.json").write_text(json.dumps(provenance))
        return target

    def read(self, store: str, relpath: str) -> str:
        return (self.path(store) / relpath).read_text()
