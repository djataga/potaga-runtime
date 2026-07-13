"""Phase-1 test suite. Runs fully offline against the MockAdapter and the
real prompt-pack repo (path via POTAGA_REPO env or ../repo)."""
from __future__ import annotations

import datetime as dt
import os
import pathlib

import pytest

from potaga_runtime.config import Config, ConfigError
from potaga_runtime.events import EventBus, EventType
from potaga_runtime.memory import AccessDenied, MemoryStores
from potaga_runtime.orchestrator import Orchestrator
from potaga_runtime.plan import PlanStore, Task, parse_tasks
from potaga_runtime.router import (AvailabilityMonitor, BudgetLedger, Router,
                                   SolUltraGovernor)
from potaga_runtime.sessions.adapters.core import MockAdapter, _MOCK_PLAN

REPO = pathlib.Path(os.environ.get("POTAGA_REPO", pathlib.Path(__file__).parent.parent.parent / "repo"))


@pytest.fixture()
def config() -> Config:
    return Config.load(REPO)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


def make_task(**kw) -> Task:
    base = dict(id="1", description="d", agent="coder", input_contract="i",
                output_contract="o", scope_boundary="ONLY x", success_criteria="s")
    base.update(kw)
    return Task(**base)


# ---------------- config ----------------
def test_config_loads_and_validates(config: Config) -> None:
    assert "backend_coding" in config.matrix["routes"]


def test_config_rejects_dead_xhigh(config: Config) -> None:
    config.matrix["routes"]["backend_coding"]["fallbacks"].append("sonnet-5@xhigh")
    with pytest.raises(ConfigError):
        config.validate()


def test_pricing_epoch_switch(config: Config) -> None:
    intro = config.pricing_for("sonnet-5", today=dt.date(2026, 8, 1))
    std = config.pricing_for("sonnet-5", today=dt.date(2026, 9, 2))
    assert intro["input_per_m"] < std["input_per_m"]


# ---------------- ledger ----------------
def test_effective_tokens_multipliers(config: Config, bus: EventBus) -> None:
    ledger = BudgetLedger(config, 100.0, bus, confirm=lambda _: True)
    base = ledger.effective_tokens(1000, "sonnet-5", "high")
    ultra = ledger.effective_tokens(1000, "gpt-5.6-sol", "ultra")
    assert base == int(1000 * 10 * 1.1)
    assert ultra == int(1000 * 10 * 3.5 * 1.1)


def test_hard_pause_declined_raises(config: Config, bus: EventBus) -> None:
    from potaga_runtime.router import BudgetExceeded
    ledger = BudgetLedger(config, ceiling_usd=0.10, bus=bus, confirm=lambda _: False)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(make_task(est_tokens_in=50000, est_tokens_out=20000), "sonnet-5", "high")


# ---------------- router ----------------
def make_router(config: Config, bus: EventBus, registered: set,
                pressure: float = 0.0) -> tuple[Router, BudgetLedger]:
    ledger = BudgetLedger(config, 100.0, bus, confirm=lambda _: True)
    monitor = AvailabilityMonitor(config, registered, bus)
    router = Router(config, monitor, bus, SolUltraGovernor(config),
                    budget_pressure=lambda: pressure)
    return router, ledger


def test_router_routes_to_available_backend(config: Config, bus: EventBus) -> None:
    router, ledger = make_router(config, bus, {"sonnet-5"})
    rp = router.plan(make_task(agent="coder"), ledger)
    assert rp.primary.backend == "sonnet-5" and not rp.degraded


def test_router_degraded_mode_logged(config: Config, bus: EventBus) -> None:
    # security_review primary is sol@ultra (non-GA/unregistered) → opus fallback
    router, ledger = make_router(config, bus, {"opus-4-8", "sonnet-5"})
    rp = router.plan(make_task(agent="reviewer", security=True), ledger)
    assert rp.primary.backend == "opus-4-8" and rp.degraded


def test_security_floor_never_below_opus(config: Config, bus: EventBus) -> None:
    # only sonnet available: security_review chain has floor opus-4-8 → sonnet must NOT qualify
    router, ledger = make_router(config, bus, {"sonnet-5"})
    with pytest.raises(RuntimeError):
        router.plan(make_task(agent="reviewer", security=True), ledger)


# ---------------- plan ----------------
def test_parse_mock_plan() -> None:
    tasks = parse_tasks(_MOCK_PLAN)
    assert [t.id for t in tasks] == ["1", "2", "3"]
    assert tasks[1].security is True and tasks[1].dependencies == ["1"]


def test_plan_scope_boundary_enforced() -> None:
    with pytest.raises(ValueError):
        make_task(scope_boundary="everything, why not").validate()


def test_plan_single_writer_renders_decision_log(tmp_path, bus: EventBus) -> None:
    plan = PlanStore(tmp_path, "p", "g", 10.0, bus)
    bus.emit(EventType.ROUTING, "test event")
    assert "test event" in plan.path.read_text()


# ---------------- memory ----------------
def test_store_grants_enforced(tmp_path) -> None:
    stores = MemoryStores(tmp_path)
    with pytest.raises(AccessDenied):
        stores.write(stores.grant("coder"), "reviews", "x.md", "nope", model="m", session_id="s")
    p = stores.write(stores.grant("coder"), "code", "x.md", "ok", model="m", session_id="s")
    assert p.read_text() == "ok"
    hist = stores.history("code", "x.md")
    assert hist and hist[0]["writer"] == "coder" and "sha256" in hist[0]


def test_store_path_escape_blocked(tmp_path) -> None:
    stores = MemoryStores(tmp_path)
    with pytest.raises(AccessDenied):
        stores.write(stores.grant("coder"), "code", "../shared/evil.md", "x", model="m", session_id="s")


# ---------------- end-to-end dry run ----------------
def test_dispatch_path_end_to_end(tmp_path, config: Config, bus: EventBus) -> None:
    orch = Orchestrator(config, tmp_path, {"sonnet-5": MockAdapter()}, bus,
                        ceiling_usd=25.0, confirm=lambda _: True, checkpoint=lambda _: True)
    plan = orch.run("test-project", "Build a REST API for tasks with JWT auth")
    assert plan.status == "complete"
    assert all(t.status == "completed" for t in plan.tasks.values())
    assert plan.spent > 0
    text = plan.path.read_text()
    assert "## Decision Log" in text and "routing" in text
    # artifacts landed in the right stores with provenance
    assert list((tmp_path / "potaga-code").rglob("artifact.md"))
    assert list((tmp_path / "potaga-code" / ".versions").rglob("*.meta.json"))
    assert list((tmp_path / "potaga-cache").rglob("status_*.json"))
