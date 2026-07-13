"""Conflict resolution & deadlock prevention — Phase 5.

Implements the Escalation & Conflict Resolution Protocols (companion doc)
and policy §B.7/§B.8:

- Conflict cards with the shared scoring formula
      score = org_value − risk_penalty − reversibility_penalty + evidence_bonus
- The bounded ladder: L0 local (≥15% margin, no hard-constraint violation,
  4 min) → L1 Orchestrator tie-breaks (2 min) → L2 Architect (5 min) →
  L3 human (unbounded). Termination is guaranteed in at most 4 hops.
- Tie-break order (§B.7): Security Override > Cost Ceiling Override >
  Deadline Override > Reversibility/Evidence Preference > Reviewer Authority.
- Security-relevant conflicts auto-escalate past local resolution, and the
  Security Override ignores score margins entirely.
- Deadlock prevention (§B.8): dependency-cycle detection via topological
  sort with lowest-priority-edge breaking, and a waiting-cycle scanner that
  preempts the lower-priority agent (state save is the caller's job).

The ladder is deterministic code. The two places judgment enters — the L2
architectural decision and the L3 human — are injected callables, so the
control plane stays prompt-free (an L2 hook may of course be backed by an
Architect LLM session).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .events import EventBus, EventType
from .plan import Task


class ConflictType(str, Enum):
    RESOURCE = "resource"
    PRIORITY = "priority"
    QUALITY_SPEED = "quality-speed"


@dataclass
class Option:
    label: str
    proposed_by: str
    impact: str = ""
    # per-agent independent scores: {agent: {org, risk, reversibility, evidence}}
    scores: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # tie-break attributes
    more_secure: bool = False
    est_cost: float = 0.0
    est_duration_min: float = 0.0
    violates: List[str] = field(default_factory=list)  # hard constraints this option violates

    def dim(self, name: str) -> float:
        vals = [s.get(name, 0.0) for s in self.scores.values()]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def score(self) -> float:
        return self.dim("org") - self.dim("risk") - self.dim("reversibility") + self.dim("evidence")


@dataclass
class Resolution:
    option: Option
    level: str                  # L0 | L1 | L2 | L3
    rule: str                   # tie-break / decision rule applied
    rationale: str


@dataclass
class ConflictCard:
    id: int
    type: ConflictType
    raised_by: str
    against: str
    summary: str
    options: List[Option]
    hard_constraints: List[str] = field(default_factory=list)
    security_relevant: bool = False
    task_id: Optional[str] = None
    status: str = "open"
    level_reached: str = "L0"
    resolution: Optional[Resolution] = None

    def render(self) -> str:
        lines = [
            f"### Conflict #{self.id:03d}",
            f"- Type: {self.type.value} · Raised by: {self.raised_by} · Against: {self.against}",
            f"- Summary: {self.summary}",
            f"- Security-relevant: {str(self.security_relevant).lower()}",
            f"- Status: {self.status} · Level reached: {self.level_reached}",
            "- Options:",
        ]
        for o in self.options:
            lines.append(f"  - {o.label} (by {o.proposed_by}) — score {o.score:.1f}"
                         + (f" · VIOLATES: {', '.join(o.violates)}" if o.violates else ""))
        if self.hard_constraints:
            lines.append(f"- Hard constraints: {'; '.join(self.hard_constraints)}")
        if self.resolution:
            r = self.resolution
            lines.append(f"- Resolution [{r.level} · {r.rule}]: {r.option.label} — {r.rationale}")
        return "\n".join(lines)


@dataclass
class LadderContext:
    """Ambient facts the tie-break rules consult."""
    budget_pressure: float = 0.0            # spent+reserved fraction of ceiling
    deadline_within_2h: bool = False
    critical_path_blocked: bool = False


ArbiterHook = Callable[[ConflictCard], Optional[Option]]
ArchitectHook = Callable[[ConflictCard], Optional[Resolution]]
HumanHook = Callable[[ConflictCard], Optional[Option]]


class ConflictLadder:
    _ids = itertools.count(1)

    def __init__(self, params: Dict, bus: EventBus,
                 context: Callable[[], LadderContext],
                 arbiter: Optional[ArbiterHook] = None,
                 architect: Optional[ArchitectHook] = None,
                 human: Optional[HumanHook] = None) -> None:
        cr = params["conflict_resolution"]
        self.margin = float(cr["local_acceptance_margin"])
        self.security_auto = bool(cr.get("security_auto_escalate", True))
        self.tiebreak_rotation = bool(cr.get("tiebreak_rotation", False))
        self.timeouts = {"L0": cr["l0_timeout_seconds"], "L1": cr["l1_timeout_seconds"],
                         "L2": cr["l2_timeout_seconds"], "L3": cr["l3_timeout_seconds"]}
        self.bus, self._ctx = bus, context
        self._arbiter, self._architect, self._human = arbiter, architect, human

    def new_card(self, **kwargs) -> ConflictCard:
        return ConflictCard(id=next(self._ids), **kwargs)

    # ---------------- the ladder ----------------
    def resolve(self, card: ConflictCard) -> ConflictCard:
        self.bus.emit(EventType.CONFLICT,
                      f"card #{card.id:03d} opened ({card.type.value}): {card.summary}",
                      task_id=card.task_id)
        for step in (self._l0, self._l1, self._l2, self._l3):
            res = step(card)
            if res is not None:
                card.resolution = res
                card.status = "resolved-local" if res.level == "L0" else "resolved-escalated"
                card.level_reached = res.level
                self.bus.emit(EventType.CONFLICT,
                              f"card #{card.id:03d} resolved at {res.level} [{res.rule}] "
                              f"→ {res.option.label}", task_id=card.task_id)
                return card
        # unreachable if a human hook exists; without one, default to safest
        safest = max(card.options, key=lambda o: (o.more_secure, o.dim("evidence")))
        card.resolution = Resolution(safest, "L3", "timeout-default",
                                     "no human hook — defaulted to safest option")
        card.status, card.level_reached = "resolved-escalated", "L3"
        return card

    # ---------------- L0: local resolution ----------------
    def _l0(self, card: ConflictCard) -> Optional[Resolution]:
        if card.security_relevant and self.security_auto:
            self.bus.emit(EventType.ESCALATION,
                          f"card #{card.id:03d}: security-relevant — auto-escalated past L0",
                          task_id=card.task_id)
            return None
        admissible = [o for o in card.options if not o.violates]
        if not admissible:
            return None
        if len(admissible) == 1:
            return Resolution(admissible[0], "L0", "single-admissible",
                              "only one option survives the hard constraints")
        ranked = sorted(admissible, key=lambda o: o.score, reverse=True)
        top, runner = ranked[0], ranked[1]
        margin = (top.score - runner.score) / top.score if top.score > 0 else 0.0
        if margin >= self.margin:
            return Resolution(top, "L0", "margin",
                              f"margin {margin:.0%} ≥ {self.margin:.0%}")
        self.bus.emit(EventType.ESCALATION,
                      f"card #{card.id:03d}: margin {margin:.0%} < {self.margin:.0%} — escalating",
                      task_id=card.task_id)
        return None

    # ---------------- L1: Orchestrator tie-breaks (§B.7 order) ----------------
    def _l1(self, card: ConflictCard) -> Optional[Resolution]:
        ctx = self._ctx()
        admissible = [o for o in card.options if not o.violates] or card.options

        # 1. Security Override — regardless of score margin
        if card.security_relevant or any(o.more_secure for o in card.options):
            secure = [o for o in admissible if o.more_secure]
            if secure:
                return Resolution(max(secure, key=lambda o: o.score), "L1", "Security Override",
                                  "auth/crypto/data involved — the more secure option wins "
                                  "regardless of score margin")

        # 2. Cost Ceiling Override — within 20% of the ceiling
        if ctx.budget_pressure >= 0.8:
            return Resolution(min(admissible, key=lambda o: o.est_cost), "L1",
                              "Cost Ceiling Override",
                              f"budget pressure {ctx.budget_pressure:.0%} — preserve runway")

        # 3. Deadline Override
        if ctx.deadline_within_2h and ctx.critical_path_blocked:
            return Resolution(min(admissible, key=lambda o: o.est_duration_min), "L1",
                              "Deadline Override",
                              "deadline within 2h on the critical path — ship the faster option")

        # 4. Reversibility / Evidence Preference
        irreversible = max(o.dim("reversibility") for o in admissible) >= 8
        if irreversible:
            return Resolution(max(admissible, key=lambda o: o.dim("evidence")), "L1",
                              "Evidence Preference",
                              "irreversible decision — stronger evidence, lower regret")
        low_rev = min(o.dim("reversibility") for o in admissible)
        most_reversible = [o for o in admissible if o.dim("reversibility") == low_rev]
        if len(most_reversible) == 1:
            return Resolution(most_reversible[0], "L1", "Reversibility Preference",
                              "reversible decision — keep future options open")

        # 5. Reviewer Authority — Coder↔Tester quality disputes go to the arbiter
        if (card.type is ConflictType.QUALITY_SPEED
                and {card.raised_by, card.against} == {"coder", "tester"}
                and self._arbiter and not self.tiebreak_rotation):
            choice = self._arbiter(card)
            if choice is not None:
                return Resolution(choice, "L1", "Reviewer Authority",
                                  "independent arbiter scored the card; decision is binding")
        return None

    # ---------------- L2: Architect ----------------
    def _l2(self, card: ConflictCard) -> Optional[Resolution]:
        if self._architect is None:
            return None
        res = self._architect(card)
        if res is not None:
            res.level, res.rule = "L2", res.rule or "architectural-fit"
        return res

    # ---------------- L3: human ----------------
    def _l3(self, card: ConflictCard) -> Optional[Resolution]:
        if self._human is None:
            return None
        self.bus.emit(EventType.HUMAN_REQUIRED,
                      f"conflict card #{card.id:03d} — user decision required",
                      task_id=card.task_id)
        choice = self._human(card)
        if choice is None:
            return None
        return Resolution(choice, "L3", "human-decision", "user decided; system resumed")


# ================================================================ deadlocks
def detect_and_break_cycles(tasks: Sequence[Task], bus: EventBus) -> List[Tuple[str, str]]:
    """Dependency-cycle detection (§B.8): topological sort over the plan;
    each cycle is broken by removing its lowest-priority edge (the edge whose
    dependent task is least critical: non-security first, then fewest
    downstream dependents). Returns the list of removed edges."""
    by_id = {t.id: t for t in tasks}
    removed: List[Tuple[str, str]] = []

    def downstream_count(tid: str) -> int:
        return sum(1 for t in tasks if tid in t.dependencies)

    while True:
        cycle = _find_cycle(by_id)
        if cycle is None:
            return removed
        # DFS stack entries follow dependency edges: (x, y) means x depends on y,
        # so the removable edge is (dep=y, task=x)
        n = len(cycle)
        edges = [(cycle[(i + 1) % n], cycle[i]) for i in range(n)]
        victim = min(edges, key=lambda e: (by_id[e[1]].security,
                                           downstream_count(e[1]),
                                           e[1]))
        dep, task_id = victim
        by_id[task_id].dependencies.remove(dep)
        removed.append(victim)
        by_id[task_id].notes = (by_id[task_id].notes +
                                f" [dependency {dep} marked optional: cycle broken]").strip()
        bus.emit(EventType.CONFLICT,
                 f"dependency cycle {' → '.join(cycle)} broken: edge {dep}→{task_id} "
                 "marked optional (lowest priority)", task_id=task_id)


def _find_cycle(by_id: Dict[str, Task]) -> Optional[List[str]]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in by_id}
    stack: List[str] = []

    def dfs(tid: str) -> Optional[List[str]]:
        color[tid] = GRAY
        stack.append(tid)
        for dep in by_id[tid].dependencies:
            if dep not in by_id:
                continue
            if color[dep] == GRAY:
                return stack[stack.index(dep):]
            if color[dep] == WHITE:
                found = dfs(dep)
                if found:
                    return found
        color[tid] = BLACK
        stack.pop()
        return None

    for tid in by_id:
        if color[tid] == WHITE:
            found = dfs(tid)
            if found:
                return found
    return None


@dataclass
class WaitEdge:
    agent: str
    waiting_on: str
    priority: float  # task criticality; higher = keep running


def scan_waiting_cycles(edges: List[WaitEdge], bus: EventBus) -> Optional[str]:
    """The 60-second waiting-cycle scan (§B.8): if agents form a wait cycle,
    return the lowest-priority agent as the preemption victim (the caller
    saves its state to the cache partition and frees the resource)."""
    waiting = {e.agent: e for e in edges}
    for start in waiting:
        seen: List[str] = []
        cur = start
        while cur in waiting:
            if cur in seen:
                cycle = seen[seen.index(cur):]
                victim = min(cycle, key=lambda a: waiting[a].priority)
                bus.emit(EventType.CONFLICT,
                         f"waiting cycle {' → '.join(cycle)} detected — preempting "
                         f"'{victim}' (lowest priority; state saved to its cache partition)")
                return victim
            seen.append(cur)
            cur = waiting[cur].waiting_on
    return None
