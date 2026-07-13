"""Config loader.

Loads config/parameters.yaml and config/routing_matrix.yaml from the Potaga
prompt-pack repo and validates the same invariants as the repo's CI
(scripts/check_consistency.py): a runtime never boots on a config the CI
would reject.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict

import yaml

ALLOWED_EFFORT = {
    "sonnet-5": {"medium", "high"},
    "opus-4-8": {"medium", "high"},
    "gpt-5.6-sol": {"base", "ultra"},
    "gpt-5.6-terra": {"base"},
    "glm-5.2": {"High"},
}


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    repo: pathlib.Path
    parameters: Dict[str, Any] = field(default_factory=dict)
    matrix: Dict[str, Any] = field(default_factory=dict)
    # Runtime-local settings (not part of the prompt pack)
    model_ids: Dict[str, str] = field(default_factory=dict)
    supports_effort_param: bool = False  # pass `effort` to the API only if the deployment supports it

    # ---------- loading ----------
    @classmethod
    def load(cls, repo_path: str | pathlib.Path, runtime_overrides: Dict[str, Any] | None = None) -> "Config":
        repo = pathlib.Path(repo_path)
        params_f = repo / "config" / "parameters.yaml"
        matrix_f = repo / "config" / "routing_matrix.yaml"
        for f in (params_f, matrix_f, repo / "prompts" / "00_shared_preamble.md"):
            if not f.exists():
                raise ConfigError(f"prompt pack incomplete: missing {f}")
        cfg = cls(
            repo=repo,
            parameters=yaml.safe_load(params_f.read_text()),
            matrix=yaml.safe_load(matrix_f.read_text()),
        )
        overrides = runtime_overrides or {}
        cfg.model_ids = overrides.get("model_ids", {"sonnet-5": "claude-sonnet-5"})
        cfg.supports_effort_param = bool(overrides.get("supports_effort_param", False))
        cfg.validate()
        return cfg

    # ---------- validation (mirrors scripts/check_consistency.py) ----------
    def validate(self) -> None:
        m = self.matrix
        xhigh_enabled = m["special_rules"].get("sonnet_xhigh_enabled", True)
        ga = {b for b, c in m["backends"].items() if c.get("ga")}
        for route, cfg in m["routes"].items():
            chain = [cfg["primary"]] + cfg.get("fallbacks", [])
            for entry in chain:
                backend, _, effort = str(entry).partition("@")
                if not xhigh_enabled and effort == "xhigh":
                    raise ConfigError(f"{route}: {entry} present while sonnet_xhigh_enabled is false")
                if backend not in m["backends"]:
                    raise ConfigError(f"{route}: unknown backend '{backend}'")
                if backend in ALLOWED_EFFORT and effort not in ALLOWED_EFFORT[backend]:
                    raise ConfigError(f"{route}: non-canonical effort '{entry}'")
            if not any(str(e).partition("@")[0] in ga for e in chain):
                raise ConfigError(f"{route}: no GA backend anywhere in the chain")
        for key in ("pricing_epoch", "quality_gates", "budget", "conflict_resolution"):
            if key not in self.parameters:
                raise ConfigError(f"parameters.yaml missing '{key}'")

    # ---------- convenience accessors ----------
    def pricing_for(self, backend: str, today: _dt.date | None = None) -> Dict[str, float]:
        """Policy §B.10 — the pricing epoch switch, driven by config."""
        p = self.parameters["pricing_epoch"]
        today = today or _dt.date.today()
        intro_until = _dt.date.fromisoformat(str(p["intro_until"]))
        if backend == "sonnet-5":
            return p["sonnet5_intro"] if today <= intro_until else p["sonnet5_standard"]
        key = {"opus-4-8": "opus48", "gpt-5.6-sol": "gpt56_sol",
               "gpt-5.6-terra": "gpt56_terra", "glm-5.2": "glm52_zai_list"}.get(backend)
        if key is None or key not in p:
            raise ConfigError(f"no pricing for backend '{backend}'")
        return p[key]

    @property
    def budget_thresholds(self) -> Dict[str, float]:
        return self.parameters["budget"]

    @property
    def defaults(self) -> Dict[str, Any]:
        return self.matrix.get("defaults", {})

    def timeout_for(self, backend: str) -> int:
        return int(self.parameters.get("timeouts_seconds", {}).get(backend, 600))
