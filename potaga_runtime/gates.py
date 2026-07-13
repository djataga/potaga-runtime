"""Quality gates — Phase 6 (policy §B.4 + guardrails spec §11).

The GateEngine is deterministic post-merge and pre-dispatch logic:

On task merge (status just arrived from the agent's cache partition):
- Coder: sandbox verification is mandatory — a `completed` without
  `sandbox_verified: true` becomes `blocked: sandbox-gate`. Scope ratio
  above `overengineering_threshold` (1.3×) becomes `blocked: scope-rejection`
  (spec §11.1's auto-reject, measured runtime-side from the agent's report).
- Tester: `completed` requires `coverage ≥ coverage_threshold` (0.70),
  else `blocked: coverage-gate` with the shortfall recorded.
- Reviewer: the verdict is parsed from the status notes. `[REJECTED: ...]`
  re-opens the coder tasks it depends on with the feedback attached (at most
  MAX_REOPENS times, then `blocked: needs-human`); `[APPROVED]` unlocks the
  docs gate.

Pre-dispatch:
- Docs tasks cannot dispatch until every reviewer task they depend on has
  an APPROVED verdict (docs finalization requires Reviewer approval).

Gate blocks are quality outcomes, not model failures: the Orchestrator must
treat them as terminal for the dispatch (no fallback-chain escalation).
"""
from __future__ import annotations

import json
import re
from typing import Dict

from .events import EventBus, EventType
from .plan import PlanStore, Task

GATE_BLOCK_PREFIXES = ("blocked: coverage-gate", "blocked: sandbox-gate",
                       "blocked: scope-rejection", "blocked: needs-human",
                       "blocked: review-rejected")

MAX_REOPENS = 2


def is_gate_block(status: str) -> bool:
    return any(status.startswith(p) for p in GATE_BLOCK_PREFIXES)


class GateEngine:
    def __init__(self, params: Dict, bus: EventBus) -> None:
        g = params["quality_gates"]
        self.coverage_threshold = float(g["coverage_threshold"])
        self.scope_threshold = float(g["overengineering_threshold"])
        self.reviewer_required = bool(g.get("reviewer_approval_required", True))
        self.bus = bus
        self.approvals: Dict[str, bool] = {}   # reviewer task id -> approved
        self.reopens: Dict[str, int] = {}      # coder task id -> count

    # ---------------- post-merge ----------------
    def on_task_merged(self, plan: PlanStore, task: Task, payload: Dict) -> None:
        if task.agent == "coder":
            self._coder_gates(task, payload)
        elif task.agent == "tester":
            self._coverage_gate(task, payload)
        elif task.agent == "reviewer":
            self._reviewer_verdict(plan, task)
        plan._flush()

    def _coder_gates(self, task: Task, payload: Dict) -> None:
        if task.status != "completed":
            return
        ratio = float(payload.get("scope_ratio", 1.0))
        if ratio > self.scope_threshold:
            task.status = "blocked: scope-rejection"
            task.notes = f"[SCOPE REJECTION: {ratio:.2f}×] exceeds {self.scope_threshold}× — trim and resubmit"
            self.bus.emit(EventType.GATE_FAIL,
                          f"scope {ratio:.2f}× > {self.scope_threshold}× — auto-rejected",
                          task_id=task.id)
            return
        if not bool(payload.get("sandbox_verified", False)):
            task.status = "blocked: sandbox-gate"
            task.notes = "completion claimed without sandbox verification — sandbox is mandatory"
            self.bus.emit(EventType.GATE_FAIL, "no sandbox verification — completion rejected",
                          task_id=task.id)
            return
        self.bus.emit(EventType.GATE_PASS, "coder gates passed (sandbox-verified, scope in bounds)",
                      task_id=task.id)

    def _coverage_gate(self, task: Task, payload: Dict) -> None:
        if task.status != "completed":
            return
        coverage = payload.get("coverage")
        if coverage is None or float(coverage) < self.coverage_threshold:
            got = "unreported" if coverage is None else f"{float(coverage):.0%}"
            task.status = "blocked: coverage-gate"
            task.notes = f"coverage {got} < {self.coverage_threshold:.0%} — uncovered paths back to Coder"
            self.bus.emit(EventType.GATE_FAIL,
                          f"coverage {got} < {self.coverage_threshold:.0%}", task_id=task.id)
            return
        self.bus.emit(EventType.GATE_PASS,
                      f"coverage {float(coverage):.0%} ≥ {self.coverage_threshold:.0%}",
                      task_id=task.id)

    def _reviewer_verdict(self, plan: PlanStore, task: Task) -> None:
        if task.status != "completed":
            return
        notes = task.notes or ""
        if re.search(r"\[APPROVED\]", notes):
            self.approvals[task.id] = True
            self.bus.emit(EventType.GATE_PASS, "review APPROVED", task_id=task.id)
            return
        m = re.search(r"\[REJECTED(?::\s*(.*?))?\]", notes)
        if not m:
            # never approve silently — an explicit verdict is required
            task.status = "blocked: review-rejected"
            task.notes = (notes + " [no explicit verdict — treated as rejection; "
                          "every review must end APPROVED or REJECTED]").strip()
            self.bus.emit(EventType.GATE_FAIL, "review ended without explicit verdict",
                          task_id=task.id)
            return
        feedback = m.group(1) or "see review report"
        self.approvals[task.id] = False
        self.bus.emit(EventType.GATE_FAIL, f"review REJECTED: {feedback}", task_id=task.id)
        # re-open the coder tasks this review depends on, with the feedback attached
        for dep_id in task.dependencies:
            dep = plan.tasks.get(dep_id)
            if dep is None or dep.agent != "coder":
                continue
            count = self.reopens.get(dep_id, 0) + 1
            self.reopens[dep_id] = count
            if count > MAX_REOPENS:
                dep.status = "blocked: needs-human"
                dep.notes = f"re-opened {MAX_REOPENS}× and rejected again — human review required"
                self.bus.emit(EventType.HUMAN_REQUIRED,
                              f"task {dep_id}: rejected {count}× — human decision required",
                              task_id=dep_id)
                continue
            dep.status = "not-started"
            dep.notes = f"[re-opened {count}/{MAX_REOPENS} by review of task {task.id}] {feedback}"
            self.bus.emit(EventType.INFO,
                          f"task {dep_id} re-opened with reviewer feedback", task_id=dep_id)
        # the review itself re-runs after the fix
        task.status = "not-started"
        task.notes = f"awaiting re-submission after rejection ({feedback})"

    # ---------------- pre-dispatch ----------------
    def can_dispatch(self, plan: PlanStore, task: Task) -> bool:
        if task.agent != "docs" or not self.reviewer_required:
            return True
        reviewer_deps = [d for d in task.dependencies
                         if d in plan.tasks and plan.tasks[d].agent == "reviewer"]
        if not reviewer_deps:
            return True
        return all(self.approvals.get(d, False) for d in reviewer_deps)


def read_status_payload(plan_cache_dir, task: Task) -> Dict:
    """The raw status payload the agent posted (the merge only lifts a subset)."""
    f = plan_cache_dir / f"cache/{task.agent}/status_{task.id}.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return {}
