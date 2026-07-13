"""Phase-6 tests: the sandbox, the tool-access matrix, token budgets, and
the §B.4 gate engine — coverage, sandbox-verification, scope rejection,
reviewer verdicts with the re-open loop, and the docs approval gate."""
from __future__ import annotations

import os
import pathlib

import pytest

from potaga_runtime.config import Config
from potaga_runtime.events import EventBus, EventType
from potaga_runtime.gates import GateEngine, is_gate_block
from potaga_runtime.memory import MemoryStores
from potaga_runtime.orchestrator import Orchestrator
from potaga_runtime.plan import PlanStore, Task
from potaga_runtime.sessions.adapters.core import MockAdapter, ToolCall, Turn
from potaga_runtime.sessions.runner import AgentRunner, SessionBuilder, tools_for
from potaga_runtime.tools.sandbox import Sandbox

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
                output_contract="o", scope_boundary="ONLY x", success_criteria="s",
                model="sonnet-5", effort="high")
    base.update(kw)
    return Task(**base)


def _orch(config, bus, tmp_path, adapters=None, ceiling=100.0):
    adapters = adapters or {b: MockAdapter(b) for b in GA}
    return Orchestrator(config, tmp_path, adapters, bus, ceiling_usd=ceiling,
                        confirm=lambda _: True, checkpoint=lambda _: True)


# ---------------- sandbox ----------------
def test_sandbox_runs_and_captures_output(tmp_path) -> None:
    res = Sandbox(tmp_path).run_python("print(6 * 7)")
    assert res.ok and res.stdout.strip() == "42"


def test_sandbox_blocks_network(tmp_path) -> None:
    res = Sandbox(tmp_path).run_python(
        "import socket\nsocket.socket()\nprint('reached')")
    assert not res.ok and "network disabled" in res.stderr
    assert "reached" not in res.stdout


def test_sandbox_timeout(tmp_path) -> None:
    res = Sandbox(tmp_path, timeout_s=1).run_python("while True: pass")
    assert res.timed_out and res.exit_code == 124


# ---------------- tool access matrix ----------------
def test_run_code_granted_per_matrix() -> None:
    assert any(t["name"] == "run_code" for t in tools_for("coder"))
    assert any(t["name"] == "run_code" for t in tools_for("tester"))
    assert not any(t["name"] == "run_code" for t in tools_for("architect"))
    assert not any(t["name"] == "run_code" for t in tools_for("research"))


class CodeRunningAdapter(MockAdapter):
    """First turn runs code in the sandbox; second posts completion."""

    def run_turn(self, system, messages, tools, effort, max_tokens):
        names = {t["name"] for t in tools}
        if len(messages) == 1:
            assert "run_code" in names
            return Turn(tool_calls=[ToolCall("run_code", {"code": "print('verified')"}, id="t1")],
                        tokens_in=400, tokens_out=60, stop_reason="tool_use")
        # tool result from run_code came back — assert it reached us, then finish
        blob = str(messages[-1])
        assert "verified" in blob and '"exit_code": 0' in blob
        return Turn(tool_calls=[
            ToolCall("save_output", {"relpath": "a.md", "content": "x"}, id="t2"),
            ToolCall("post_status", {"status": "completed", "sandbox_verified": True,
                                     "scope_ratio": 1.0}, id="t3")],
            tokens_in=400, tokens_out=80, stop_reason="tool_use")


def test_run_code_round_trip(tmp_path, config, bus) -> None:
    stores = MemoryStores(tmp_path)
    runner = AgentRunner(stores, bus, config)
    task = make_task(id="7")
    runner.run(task, CodeRunningAdapter("sonnet-5"), "sys",
               SessionBuilder(config).opening_message(task))
    assert stores.exists("cache", "cache/coder/status_7.json")


# ---------------- token budget ----------------
class VerboseAdapter(MockAdapter):
    def run_turn(self, system, messages, tools, effort, max_tokens):
        return Turn(tool_calls=[ToolCall("save_output",
                                         {"relpath": "x.md", "content": "y"}, id="t1")],
                    tokens_in=1000, tokens_out=5000, stop_reason="tool_use")


def test_token_budget_interrupts(tmp_path, config, bus) -> None:
    stores = MemoryStores(tmp_path)
    runner = AgentRunner(stores, bus, config)
    task = make_task(id="8", est_tokens_out=1000)  # budget = 3000
    runner.run(task, VerboseAdapter("sonnet-5"), "sys",
               SessionBuilder(config).opening_message(task))
    import json
    payload = json.loads(stores.read("cache", "cache/coder/status_8.json"))
    assert payload["status"] == "blocked: token-budget"
    assert any(e.type == EventType.BUDGET and "token budget" in e.detail for e in bus.history)


# ---------------- gate engine (unit) ----------------
def _plan(tmp_path, bus, tasks) -> PlanStore:
    plan = PlanStore(tmp_path, "p", "g", 100.0, bus)
    plan.set_tasks(tasks)
    return plan


def test_coverage_gate_blocks_and_passes(config, bus, tmp_path) -> None:
    g = GateEngine(config.parameters, bus)
    t = make_task(agent="tester", id="t1")
    t.status = "completed"
    g.on_task_merged(_plan(tmp_path, bus, [t]), t, {"coverage": 0.42})
    assert t.status == "blocked: coverage-gate" and is_gate_block(t.status)
    t.status = "completed"
    g.on_task_merged(_plan(tmp_path, bus, [t]), t, {"coverage": 0.80})
    assert t.status == "completed"


def test_sandbox_and_scope_gates_on_coder(config, bus, tmp_path) -> None:
    g = GateEngine(config.parameters, bus)
    t = make_task(id="c1")
    t.status = "completed"
    g.on_task_merged(_plan(tmp_path, bus, [t]), t, {"sandbox_verified": False})
    assert t.status == "blocked: sandbox-gate"
    t.status = "completed"
    g.on_task_merged(_plan(tmp_path, bus, [t]), t,
                     {"sandbox_verified": True, "scope_ratio": 1.55})
    assert t.status == "blocked: scope-rejection"
    assert "[SCOPE REJECTION: 1.55×]" in t.notes


def test_reviewer_rejection_reopens_coder_then_needs_human(config, bus, tmp_path) -> None:
    g = GateEngine(config.parameters, bus)
    coder = make_task(id="c1")
    coder.status = "completed"
    review = make_task(id="r1", agent="reviewer", dependencies=["c1"])
    plan = _plan(tmp_path, bus, [coder, review])
    for round_no in (1, 2):
        review.status = "completed"
        review.notes = "[REJECTED: injection risk]"
        g.on_task_merged(plan, review, {})
        assert coder.status == "not-started"
        assert f"re-opened {round_no}/2" in coder.notes
        assert review.status == "not-started"  # review re-runs after the fix
        coder.status = "completed"
    # third rejection → human
    review.status = "completed"
    review.notes = "[REJECTED: still present]"
    g.on_task_merged(plan, review, {})
    assert coder.status == "blocked: needs-human"
    assert any(e.type == EventType.HUMAN_REQUIRED for e in bus.history)


def test_silent_review_is_treated_as_rejection(config, bus, tmp_path) -> None:
    g = GateEngine(config.parameters, bus)
    review = make_task(id="r1", agent="reviewer")
    review.status = "completed"
    review.notes = "looks fine to me"  # no explicit verdict
    g.on_task_merged(_plan(tmp_path, bus, [review]), review, {})
    assert review.status == "blocked: review-rejected"


def test_docs_dispatch_requires_reviewer_approval(config, bus, tmp_path) -> None:
    g = GateEngine(config.parameters, bus)
    review = make_task(id="r1", agent="reviewer")
    docs = make_task(id="d1", agent="docs", dependencies=["r1"])
    plan = _plan(tmp_path, bus, [review, docs])
    assert not g.can_dispatch(plan, docs)
    review.status = "completed"
    review.notes = "[APPROVED] checks stated"
    g.on_task_merged(plan, review, {})
    assert g.can_dispatch(plan, docs)


# ---------------- end-to-end ----------------
def test_low_coverage_blocks_without_chain_escalation(config, bus, tmp_path) -> None:
    adapters = {b: MockAdapter(b) for b in GA}
    # tester routes to glm-5.2 (Phase-3 CQP); scripted to report low coverage
    adapters["glm-5.2"] = MockAdapter("glm-5.2", script=["low-coverage"])
    plan = _orch(config, bus, tmp_path, adapters).run("p", "Build a REST API with auth")
    tester = next(t for t in plan.tasks.values() if t.agent == "tester")
    assert tester.status == "blocked: coverage-gate"
    # gate block is a quality outcome — the model chain was NOT walked
    assert not any(e.type == EventType.ESCALATION and e.task_id == tester.id
                   for e in bus.history)
    assert plan.status == "review"


def test_clean_run_passes_all_gates(config, bus, tmp_path) -> None:
    plan = _orch(config, bus, tmp_path).run("p", "Build a REST API with auth")
    assert plan.status == "complete"
    passes = [e for e in bus.history if e.type == EventType.GATE_PASS]
    assert any("coverage" in e.detail for e in passes)
    assert any("sandbox-verified" in e.detail for e in passes)
