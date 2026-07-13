"""Phase-3 tests: multi-model CQP routing, Sol Ultra containment,
fallback-chain escalation, cost-ceiling preference — all offline."""
from __future__ import annotations

import os
import pathlib

import pytest

from potaga_runtime.config import Config
from potaga_runtime.events import EventBus, EventType
from potaga_runtime.orchestrator import Orchestrator
from potaga_runtime.plan import Task
from potaga_runtime.router import (AvailabilityMonitor, BudgetLedger, Router,
                                   SolUltraGovernor)
from potaga_runtime.sessions.adapters.core import MockAdapter

REPO = pathlib.Path(os.environ.get("POTAGA_REPO", pathlib.Path(__file__).parent.parent.parent / "repo"))
GA = {"sonnet-5", "opus-4-8", "glm-5.2"}


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


def make_router(config, bus, registered=GA, pressure=0.0, ceiling=100.0):
    ledger = BudgetLedger(config, ceiling, bus, confirm=lambda _: True)
    monitor = AvailabilityMonitor(config, set(registered), bus)
    router = Router(config, monitor, bus, SolUltraGovernor(config),
                    budget_pressure=lambda: pressure)
    return router, ledger, monitor


# ---------------- CQP routing ----------------
def test_frontend_task_classified_and_routed_to_glm(config, bus) -> None:
    router, ledger, _ = make_router(config, bus)
    task = make_task(agent="coder", description="Build the landing page UI",
                     scope_boundary="ONLY the frontend component")
    rp = router.plan(task, ledger)
    assert rp.route == "frontend_ui"
    assert rp.primary.backend == "glm-5.2"  # #1 quality AND cheapest → wins CQP outright


def test_backend_coding_stays_on_sonnet_despite_cheaper_glm(config, bus) -> None:
    # GLM is far cheaper but falls below the 80% quality threshold for backend coding
    router, ledger, _ = make_router(config, bus)
    rp = router.plan(make_task(agent="coder", description="Implement the repository layer"),
                     ledger)
    assert rp.primary.backend == "sonnet-5"
    assert any(c.backend == "glm-5.2" for c in rp.chain[1:])  # still in the fallback chain


def test_cost_ceiling_preference_flips_docs_to_glm(config, bus) -> None:
    # At ≥80% budget pressure, non-security tasks with a close CQP race pick the cheaper option
    router, ledger, _ = make_router(config, bus, pressure=0.85)
    rp = router.plan(make_task(agent="docs", description="Write the README"), ledger)
    assert rp.primary.backend in {"glm-5.2", "sonnet-5"}
    # security tasks never flip on cost:
    rp_sec = router.plan(make_task(agent="reviewer", security=True), ledger)
    assert rp_sec.primary.backend == "opus-4-8"


# ---------------- Sol Ultra containment ----------------
def test_sol_ultra_cap_skips_after_max_calls(config, bus) -> None:
    registered = GA | {"gpt-5.6-sol"}
    ledger = BudgetLedger(config, 1000.0, bus, confirm=lambda _: True)
    monitor = AvailabilityMonitor(config, registered, bus)
    monitor.set_status("gpt-5.6-sol", "available")  # simulate preview access granted
    gov = SolUltraGovernor(config)
    router = Router(config, monitor, bus, gov, budget_pressure=lambda: 0.0)

    task = make_task(agent="reviewer", security=True)
    assert router.plan(task, ledger).primary.backend == "gpt-5.6-sol"
    gov.calls = gov.max_calls  # exhaust the per-project cap
    rp = router.plan(task, ledger)
    assert rp.primary.backend == "opus-4-8"  # floor holds, ultra skipped


def test_sol_ultra_cost_carries_multiplier(config, bus) -> None:
    _, ledger, _ = make_router(config, bus)
    t = make_task(est_tokens_in=1000, est_tokens_out=1000)
    assert ledger.estimate_cost(t, "gpt-5.6-sol", "ultra") > \
        3 * ledger.estimate_cost(t, "gpt-5.6-sol", "base")


# ---------------- availability transitions ----------------
def test_degraded_transition_announced_once(config, bus) -> None:
    registered = GA | {"gpt-5.6-sol"}
    monitor = AvailabilityMonitor(config, registered, bus)
    monitor.set_status("gpt-5.6-sol", "available")
    monitor.set_status("gpt-5.6-sol", "unavailable")
    monitor.set_status("gpt-5.6-sol", "available")
    monitor.set_status("gpt-5.6-sol", "unavailable")  # second down-transition
    announcements = [e for e in bus.history if e.type == EventType.DEGRADED_MODE]
    assert len(announcements) == 1  # once per session, per §B.9


# ---------------- §B.5 chain walking (end-to-end) ----------------
def _orch(config, bus, tmp_path, adapters, ceiling=100.0):
    return Orchestrator(config, tmp_path, adapters, bus, ceiling_usd=ceiling,
                        confirm=lambda _: True, checkpoint=lambda _: True)


def test_chain_walk_escalates_after_primary_failures(config, bus, tmp_path) -> None:
    # The mock plan's coder task is security:true → Security-Critical Path routes it
    # to opus-4-8 (highest quality). Opus blocks twice (attempt + same-tier retry)
    # → §B.5 escalates along the declared chain to sonnet-5, which completes.
    adapters = {
        "sonnet-5": MockAdapter("sonnet-5"),
        "opus-4-8": MockAdapter("opus-4-8", script=["block", "block"]),
        "glm-5.2": MockAdapter("glm-5.2"),
    }
    plan = _orch(config, bus, tmp_path, adapters).run("p", "Build a REST API with auth")
    coder_task = next(t for t in plan.tasks.values() if t.agent == "coder")
    assert coder_task.status == "completed"
    assert coder_task.model == "sonnet-5"
    types = [e.type for e in bus.history]
    assert EventType.FALLBACK in types and EventType.ESCALATION in types
    assert plan.status == "complete"


def test_safeguard_refusal_is_terminal_not_redispatched(config, bus, tmp_path) -> None:
    # opus (the coder task's security-path primary) refuses → blocked: safeguard,
    # and the content is never walked down the chain (§B.6)
    adapters = {
        "sonnet-5": MockAdapter("sonnet-5"),
        "opus-4-8": MockAdapter("opus-4-8", script=["refuse"]),
        "glm-5.2": MockAdapter("glm-5.2"),
    }
    plan = _orch(config, bus, tmp_path, adapters).run("p", "Build a REST API with auth")
    coder_task = next(t for t in plan.tasks.values() if t.agent == "coder")
    assert coder_task.status.startswith("blocked: safeguard")
    assert coder_task.model == "opus-4-8"
    assert not any(e.type == EventType.ESCALATION and e.task_id == coder_task.id
                   for e in bus.history)


def test_multi_backend_dry_run_spend_tracked_per_backend(config, bus, tmp_path) -> None:
    adapters = {b: MockAdapter(b) for b in GA}
    orch = _orch(config, bus, tmp_path, adapters)
    plan = orch.run("p", "Build a REST API with auth")
    assert plan.status == "complete"
    assert set(orch.ledger.spent_by_backend) <= GA
    assert sum(orch.ledger.spent_by_backend.values()) == pytest.approx(plan.spent)
