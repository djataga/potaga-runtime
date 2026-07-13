"""Phase-4 tests: optimistic concurrency, immutable versions, session
attachments, context eviction/recovery, and cross-session resume."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from potaga_runtime.config import Config
from potaga_runtime.events import EventBus, EventType
from potaga_runtime.memory import (MemoryStores, OCCExhausted,
                                   PreconditionFailed, FilesystemBackend)
from potaga_runtime.orchestrator import Orchestrator
from potaga_runtime.plan import parse_rendered_plan
from potaga_runtime.sessions.adapters.core import MockAdapter
from potaga_runtime.sessions.runner import AgentRunner, SessionBuilder

REPO = pathlib.Path(os.environ.get("POTAGA_REPO", pathlib.Path(__file__).parent.parent.parent / "repo"))
GA = {"sonnet-5", "opus-4-8", "glm-5.2"}


@pytest.fixture()
def config() -> Config:
    return Config.load(REPO)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


def _orch(config, bus, tmp_path, adapters=None, ceiling=100.0):
    adapters = adapters or {b: MockAdapter(b) for b in GA}
    return Orchestrator(config, tmp_path, adapters, bus, ceiling_usd=ceiling,
                        confirm=lambda _: True, checkpoint=lambda _: True)


# ---------------- optimistic concurrency ----------------
def test_write_if_match_rejects_stale_hash(tmp_path) -> None:
    be = FilesystemBackend(tmp_path)
    prov = {"writer": "docs", "model": "m", "session": "s"}
    h1 = be.write("shared", "conventions.md", "v1", prov)
    be.write("shared", "conventions.md", "v2", prov, expected_hash=h1)
    with pytest.raises(PreconditionFailed):
        be.write("shared", "conventions.md", "v3-stale", prov, expected_hash=h1)


def test_occ_update_retries_through_interleaved_writer(tmp_path) -> None:
    stores = MemoryStores(tmp_path)
    docs, research = stores.grant("docs"), stores.grant("research")
    stores.write(docs, "shared", "conventions.md", "base",
                 model="m", session_id="s1")
    interfered = {"done": False}

    def update(current: str) -> str:
        if not interfered["done"]:  # simulate a concurrent writer mid read-modify-write
            interfered["done"] = True
            stores.write(research, "shared", "conventions.md", current + "\n+research",
                         model="m", session_id="s2")
        return current + "\n+docs"

    stores.occ_update(docs, "shared", "conventions.md", update,
                      model="m", session_id="s1")
    final = stores.read("shared", "conventions.md")
    assert "+research" in final and "+docs" in final  # nothing lost


def test_occ_exhaustion_raises_for_escalation(tmp_path) -> None:
    stores = MemoryStores(tmp_path)
    docs, research = stores.grant("docs"), stores.grant("research")
    stores.write(docs, "shared", "hot.md", "base", model="m", session_id="s1")

    def always_interfered(current: str) -> str:
        stores.write(research, "shared", "hot.md", current + "!",
                     model="m", session_id="s2")
        return current + "?"

    with pytest.raises(OCCExhausted):
        stores.occ_update(docs, "shared", "hot.md", always_interfered,
                          model="m", session_id="s1", max_retries=3)


def test_immutable_version_history_with_provenance(tmp_path) -> None:
    stores = MemoryStores(tmp_path)
    docs = stores.grant("docs")
    for i in range(3):
        stores.write(docs, "docs", "README.md", f"v{i}", model="sonnet-5@medium",
                     session_id=f"s{i}")
    hist = stores.history("docs", "README.md")
    assert len(hist) == 3
    assert [h["session"] for h in hist] == ["s0", "s1", "s2"]
    assert all("sha256" in h and h["writer"] == "docs" for h in hist)


# ---------------- session attachment ----------------
def test_attachments_render_per_agent_access(tmp_path, config) -> None:
    stores = MemoryStores(tmp_path)
    att = {a["store"]: a["access"] for a in stores.attachments("coder")}
    assert att["potaga-code"] == "read_write"
    assert att["potaga-reviews"] == "read_only"
    assert att["potaga-cache"] == "read_write"
    opening = SessionBuilder(config).opening_message(
        __import__("potaga_runtime.plan", fromlist=["Task"]).Task(
            id="1", description="d", agent="coder", input_contract="i",
            output_contract="o", scope_boundary="ONLY x", success_criteria="s"),
        attachments=stores.attachments("coder"))
    assert "potaga-code [read_write]" in opening


# ---------------- context eviction + recovery ----------------
class ChattyAdapter(MockAdapter):
    """Never posts status; floods the history so eviction must trigger."""

    def run_turn(self, system, messages, tools, effort, max_tokens):
        from potaga_runtime.sessions.adapters.core import ToolCall, Turn
        return Turn(tool_calls=[ToolCall("save_output",
                                         {"relpath": f"chunk_{len(messages)}.md",
                                          "content": "x" * 4000}, id="t1")],
                    tokens_in=500, tokens_out=100, stop_reason="tool_use")


def test_context_eviction_saves_scratch_then_compacts(tmp_path, config, bus) -> None:
    from potaga_runtime.memory import MemoryStores
    from potaga_runtime.plan import Task
    stores = MemoryStores(tmp_path)
    runner = AgentRunner(stores, bus, config, context_limit=3000)
    task = Task(id="9", description="d", agent="coder", input_contract="i",
                output_contract="o", scope_boundary="ONLY x", success_criteria="s",
                model="sonnet-5", effort="high")
    runner.run(task, ChattyAdapter("sonnet-5"),
               "system", SessionBuilder(config).opening_message(task))
    assert stores.exists("cache", "cache/coder/scratch_9.json")
    assert any(e.type == EventType.INFO and "compacted" in e.detail for e in bus.history)
    # ends blocked (never posted status) — the scratch is what recovery uses
    assert stores.exists("cache", "cache/coder/status_9.json")


def test_recovery_scratch_injected_into_next_session(tmp_path, config, bus) -> None:
    from potaga_runtime.plan import Task
    stores = MemoryStores(tmp_path)
    runner = AgentRunner(stores, bus, config)
    task = Task(id="9", description="d", agent="coder", input_contract="i",
                output_contract="o", scope_boundary="ONLY x", success_criteria="s")
    stores.write(stores.grant("coder"), "cache", "scratch_9.json",
                 json.dumps({"note": "resume from step 3"}), model="m", session_id="s")
    opening = SessionBuilder(config).opening_message(
        task, recovery=runner.load_scratch(task))
    assert "RECOVERY PROTOCOL" in opening and "resume from step 3" in opening


# ---------------- cross-session resume ----------------
def test_resume_completes_interrupted_project(tmp_path, config, bus) -> None:
    # Session 1: coder blocks (chain exhausted on every backend)
    blocking = {b: MockAdapter(b, script=["block"] * 8) for b in GA}
    blocking["sonnet-5"] = MockAdapter("sonnet-5", script=["complete"] + ["block"] * 8)
    plan1 = _orch(config, bus, tmp_path, blocking).run("p", "Build a REST API with auth")
    assert plan1.status == "review"
    coder1 = next(t for t in plan1.tasks.values() if t.agent == "coder")
    assert coder1.status.startswith("blocked")

    # Session 2 (fresh orchestrator over the same workspace): retry blocked → completes
    bus2 = EventBus()
    orch2 = _orch(config, bus2, tmp_path)  # all mocks complete now
    plan2 = orch2.resume(retry_blocked=True)
    assert plan2.status == "complete"
    assert all(t.status == "completed" for t in plan2.tasks.values())
    # spend carried across sessions
    assert plan2.spent >= plan1.spent


def test_resume_never_retries_safeguard_blocks(tmp_path, config, bus) -> None:
    adapters = {b: MockAdapter(b) for b in GA}
    adapters["opus-4-8"] = MockAdapter("opus-4-8", script=["refuse"])
    plan1 = _orch(config, bus, tmp_path, adapters).run("p", "Build a REST API with auth")
    coder1 = next(t for t in plan1.tasks.values() if t.agent == "coder")
    assert coder1.status.startswith("blocked: safeguard")

    plan2 = _orch(config, EventBus(), tmp_path).resume(retry_blocked=True)
    coder2 = next(t for t in plan2.tasks.values() if t.agent == "coder")
    assert coder2.status.startswith("blocked: safeguard")  # §B.6 holds across sessions


def test_parse_rendered_plan_roundtrip(tmp_path, config, bus) -> None:
    plan = _orch(config, bus, tmp_path).run("roundtrip", "Build a REST API with auth")
    parsed = parse_rendered_plan(plan.path.read_text())
    assert parsed["meta"]["project"] == "roundtrip"
    assert parsed["meta"]["spent"] == pytest.approx(plan.spent, abs=0.01)
    statuses = {t.id: t.status for t in parsed["tasks"]}
    assert all(s == "completed" for s in statuses.values())
    models = {t.id: t.model for t in parsed["tasks"]}
    assert all(models.values())
