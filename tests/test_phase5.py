"""Phase-5 tests: the conflict ladder end to end — L0 margins, hard
constraints, the §B.7 tie-break order, L2/L3 hooks — plus deadlock
prevention and the plan's Conflict Log."""
from __future__ import annotations

import os
import pathlib

import pytest

from potaga_runtime.config import Config
from potaga_runtime.conflicts import (ConflictCard, ConflictLadder, ConflictType,
                                      LadderContext, Option, Resolution, WaitEdge,
                                      detect_and_break_cycles, scan_waiting_cycles)
from potaga_runtime.events import EventBus, EventType
from potaga_runtime.orchestrator import Orchestrator
from potaga_runtime.plan import Task
from potaga_runtime.sessions.adapters.core import MockAdapter

REPO = pathlib.Path(os.environ.get("POTAGA_REPO", pathlib.Path(__file__).parent.parent.parent / "repo"))
GA = {"sonnet-5", "opus-4-8", "glm-5.2"}


@pytest.fixture()
def config() -> Config:
    return Config.load(REPO)


@pytest.fixture()
def bus() -> EventBus:
    return EventBus()


def ladder(config, bus, pressure=0.0, deadline=False, critical=False, **hooks) -> ConflictLadder:
    return ConflictLadder(config.parameters, bus,
                          context=lambda: LadderContext(budget_pressure=pressure,
                                                        deadline_within_2h=deadline,
                                                        critical_path_blocked=critical),
                          **hooks)


def opt(label, org, risk, rev, ev, agents=("a", "b"), **kw) -> Option:
    return Option(label=label, proposed_by=agents[0],
                  scores={a: {"org": org, "risk": risk, "reversibility": rev, "evidence": ev}
                          for a in agents}, **kw)


def card(l: ConflictLadder, options, *, type=ConflictType.PRIORITY,
         raised="architect", against="research", security=False,
         constraints=None) -> ConflictCard:
    return l.new_card(type=type, raised_by=raised, against=against,
                      summary="test conflict", options=options,
                      hard_constraints=constraints or [],
                      security_relevant=security)


# ---------------- L0 ----------------
def test_l0_accepts_on_margin(config, bus) -> None:
    l = ladder(config, bus)
    c = l.resolve(card(l, [opt("strong", 8, 2, 1, 5), opt("weak", 4, 4, 3, 1)]))
    assert c.level_reached == "L0" and c.resolution.rule == "margin"
    assert c.resolution.option.label == "strong"
    assert c.status == "resolved-local"


def test_l0_escalates_below_margin(config, bus) -> None:
    l = ladder(config, bus)
    # near-identical scores → margin < 15% → L1; reversibility differs → Reversibility Preference
    c = l.resolve(card(l, [opt("A", 7, 2, 2, 3), opt("B", 7, 2, 1, 2.8)]))
    assert c.level_reached == "L1"
    assert c.resolution.rule == "Reversibility Preference"
    assert c.resolution.option.label == "B"  # lower reversibility penalty = more reversible


def test_l0_hard_constraint_violation_escalates_despite_margin(config, bus) -> None:
    # spec Conflict #001: 65% margin, but the losing-side option violates a
    # security hard constraint → auto-escalated, Security Override applies
    l = ladder(config, bus)
    fix_now = opt("fix SQL injection now", 8, 2, 1, 5, agents=("tester",), more_secure=True)
    ship = opt("ship as-is, log debt", 6, 7, 5, 3, agents=("coder",),
               violates=["security review is mandatory before delivery"])
    c = l.resolve(card(l, [fix_now, ship], type=ConflictType.QUALITY_SPEED,
                       raised="tester", against="coder", security=True,
                       constraints=["security review is mandatory before delivery"]))
    assert c.level_reached == "L1"
    assert c.resolution.rule == "Security Override"
    assert c.resolution.option.label == "fix SQL injection now"


def test_security_relevant_skips_l0_even_with_huge_margin(config, bus) -> None:
    l = ladder(config, bus)
    c = l.resolve(card(l, [opt("secure", 9, 1, 1, 5, more_secure=True),
                           opt("fast", 2, 8, 8, 0)], security=True))
    assert c.level_reached == "L1" and c.resolution.rule == "Security Override"


# ---------------- L1 tie-break order ----------------
def test_cost_ceiling_override_at_pressure(config, bus) -> None:
    l = ladder(config, bus, pressure=0.85)
    cheap = opt("cheap", 7, 2, 2, 3, est_cost=1.0)
    pricey = opt("pricey", 7, 2, 2, 3.2, est_cost=9.0)
    c = l.resolve(card(l, [cheap, pricey]))
    assert c.resolution.rule == "Cost Ceiling Override"
    assert c.resolution.option.label == "cheap"


def test_deadline_override_on_critical_path(config, bus) -> None:
    l = ladder(config, bus, deadline=True, critical=True)
    fast = opt("fast", 7, 2, 2, 3, est_duration_min=10)
    slow = opt("slow", 7, 2, 2, 3.2, est_duration_min=90)
    c = l.resolve(card(l, [fast, slow]))
    assert c.resolution.rule == "Deadline Override"
    assert c.resolution.option.label == "fast"


def test_evidence_preference_for_irreversible_decisions(config, bus) -> None:
    l = ladder(config, bus)
    # margin < 15% AND both irreversible (reversibility ≥ 8) → strongest evidence wins
    a = opt("weak evidence", 9, 1, 9, 3)      # score 2.0
    b = opt("strong evidence", 9, 1, 9, 3.2)  # score 2.2 → margin 9%
    c = l.resolve(card(l, [a, b]))
    assert c.resolution.rule == "Evidence Preference"
    assert c.resolution.option.label == "strong evidence"


def test_reviewer_authority_arbiter_is_binding(config, bus) -> None:
    a = opt("coder view", 7, 2, 5, 3, agents=("coder",))
    b = opt("tester view", 7, 2, 5, 3, agents=("tester",))
    l = ladder(config, bus, arbiter=lambda card: b)
    c = l.resolve(card(l, [a, b], type=ConflictType.QUALITY_SPEED,
                       raised="coder", against="tester"))
    assert c.resolution.rule == "Reviewer Authority"
    assert c.resolution.option.label == "tester view"


# ---------------- L2 / L3 ----------------
def test_l2_architect_hook_decides(config, bus) -> None:
    a = opt("A", 7, 2, 5, 3)
    b = opt("B", 7, 2, 5, 3)
    l = ladder(config, bus,
               architect=lambda card: Resolution(card.options[0], "L2", "architectural-fit",
                                                 "fits ADR-002"))
    c = l.resolve(card(l, [a, b]))
    assert c.level_reached == "L2" and c.resolution.option.label == "A"


def test_l3_human_required_event_and_decision(config, bus) -> None:
    a = opt("A", 7, 2, 5, 3)
    b = opt("B", 7, 2, 5, 3)
    l = ladder(config, bus, human=lambda card: card.options[1])
    c = l.resolve(card(l, [a, b]))
    assert c.level_reached == "L3" and c.resolution.option.label == "B"
    assert any(e.type == EventType.HUMAN_REQUIRED for e in bus.history)


# ---------------- deadlock prevention ----------------
def make_task(tid, deps, security=False) -> Task:
    return Task(id=tid, description="d", agent="coder", input_contract="i",
                output_contract="o", scope_boundary="ONLY x", success_criteria="s",
                dependencies=list(deps), security=security)


def test_dependency_cycle_broken_at_lowest_priority_edge(bus) -> None:
    # 1→2→3→1 with task 2 security-critical: the broken edge must not target task 2
    tasks = [make_task("1", ["3"]), make_task("2", ["1"], security=True), make_task("3", ["2"])]
    removed = detect_and_break_cycles(tasks, bus)
    assert len(removed) == 1
    assert removed[0][1] != "2"  # security task keeps its dependency
    # graph is now acyclic: a full topological order exists
    assert detect_and_break_cycles(tasks, bus) == []
    assert any(e.type == EventType.CONFLICT and "cycle" in e.detail for e in bus.history)


def test_waiting_cycle_preempts_lowest_priority(bus) -> None:
    victim = scan_waiting_cycles([WaitEdge("coder", "tester", priority=0.9),
                                  WaitEdge("tester", "coder", priority=0.4)], bus)
    assert victim == "tester"
    assert scan_waiting_cycles([WaitEdge("coder", "tester", priority=1.0)], bus) is None


# ---------------- plan integration ----------------
def test_resolved_card_lands_in_conflict_log(config, bus, tmp_path) -> None:
    orch = Orchestrator(config, tmp_path, {b: MockAdapter(b) for b in GA}, bus,
                        ceiling_usd=100.0, confirm=lambda _: True, checkpoint=lambda _: True)
    plan = orch.run("p", "Build a REST API with auth")
    c = orch.ladder.new_card(
        type=ConflictType.QUALITY_SPEED, raised_by="tester", against="coder",
        summary="2 failing edge-case tests on auth endpoint",
        options=[opt("fix now", 8, 2, 1, 5, agents=("tester",), more_secure=True),
                 opt("ship as-is", 6, 7, 5, 3, agents=("coder",),
                     violates=["security review mandatory"])],
        security_relevant=True)
    orch.resolve_conflict(plan, c)
    text = plan.path.read_text()
    assert "## Conflict Log" in text
    assert "Security Override" in text and "fix now" in text
