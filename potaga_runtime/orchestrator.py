"""Decomposer + Orchestrator — Phase 3.

Decomposer: unchanged — the ONE LLM call, §A prompt verbatim.

Orchestrator dispatch now implements §B.5 in full over the router's
RoutePlan: try the CQP-chosen primary, retry once on the same tier, then
walk the fallback chain tier by tier (FALLBACK → ESCALATION events), until
completed / safeguard-blocked / chain exhausted. Sol Ultra dispatches go
through the governor (serialized, capped per project).
"""
from __future__ import annotations

import pathlib
import re
from typing import Callable, Dict, List

from .config import Config
from .events import EventBus, EventType
from .memory import MemoryStores
from .plan import PlanStore, Task, parse_rendered_plan, parse_tasks
from .router import (AvailabilityMonitor, BudgetLedger, Candidate, Router,
                     SolUltraGovernor)
from .sessions.adapters.core import Adapter
from .sessions.runner import AgentRunner, SessionBuilder


class Decomposer:
    def __init__(self, config: Config, adapter: Adapter, bus: EventBus) -> None:
        self.adapter, self.bus = adapter, bus
        text = (config.repo / "prompts" / "07_orchestrator.md").read_text()
        m = re.search(r"```xml\n(.*?)```", text, re.S)
        if not m:
            raise RuntimeError("could not extract §A decomposition prompt from 07_orchestrator.md")
        self._system = m.group(1).strip()

    def decompose(self, request: str) -> List[Task]:
        self.bus.emit(EventType.INFO, "decomposing request (sonnet-5 @ high, single LLM call)")
        turn = self.adapter.run_turn(
            system=self._system,
            messages=[{"role": "user", "content": f"DECOMPOSE the following request:\n\n{request}"}],
            tools=[], effort="high", max_tokens=4096)
        tasks = parse_tasks(turn.text)
        self.bus.emit(EventType.INFO, f"decomposed into {len(tasks)} subtasks")
        return tasks


class Orchestrator:
    SAME_TIER_RETRIES = 1  # §B.5: retry once same tier, then escalate

    def __init__(self, config: Config, workspace: pathlib.Path, adapters: Dict[str, Adapter],
                 bus: EventBus, ceiling_usd: float,
                 confirm: Callable[[str], bool], checkpoint: Callable[[str], bool]) -> None:
        self.config, self.bus = config, bus
        self.stores = MemoryStores(workspace)
        self.adapters = adapters
        self.monitor = AvailabilityMonitor(config, set(adapters), bus)
        self.governor = SolUltraGovernor(config)
        self.ledger = BudgetLedger(config, ceiling_usd, bus, confirm)
        self.router = Router(config, self.monitor, bus, self.governor,
                             budget_pressure=self.ledger.pressure)
        self.builder = SessionBuilder(config)
        self.runner = AgentRunner(self.stores, bus, config)
        self.ceiling = ceiling_usd
        self._checkpoint = checkpoint

    # ---------- the loop ----------
    def run(self, project: str, request: str) -> PlanStore:
        plan = PlanStore(self.stores.path("shared"), project, request, self.ceiling, self.bus)
        decomposer = Decomposer(self.config, self.adapters["sonnet-5"], self.bus)
        plan.set_tasks(decomposer.decompose(request))

        while not plan.all_terminal():
            ready = plan.ready_tasks()
            if not ready:
                self.bus.emit(EventType.INFO,
                              "no dispatchable tasks remain (blocked dependencies) — stopping")
                break
            for task in ready:  # sequential; true parallel dispatch is a later phase
                self._dispatch(plan, task)
                if task.agent == "architect" and task.status == "completed":
                    self._human_architecture_gate(plan, task)

        plan.status = "complete" if all(t.status == "completed" for t in plan.tasks.values()) else "review"
        plan._flush()
        return plan

    # ---------- cross-session resume (spec §9.6 recovery / roadmap Phase 4) ----------
    def resume(self, retry_blocked: bool = False) -> PlanStore:
        """Load an existing MULTI_AGENT_PLAN.md from the workspace and continue.

        completed stays completed; in_progress from a dead session resets to
        not-started (the runner's recovery scratch, if any, is injected into
        the new session); blocked resets only with retry_blocked=True."""
        plan_file = self.stores.path("shared") / "MULTI_AGENT_PLAN.md"
        if not plan_file.exists():
            raise FileNotFoundError(f"nothing to resume: {plan_file} does not exist")
        parsed = parse_rendered_plan(plan_file.read_text())
        meta = parsed["meta"]
        self.bus.emit(EventType.INFO,
                      f"resuming project '{meta.get('project')}' from persisted plan")
        plan = PlanStore(self.stores.path("shared"), meta.get("project", "resumed"),
                         meta.get("goal", ""), self.ceiling, self.bus)
        plan.spent = float(meta.get("spent", 0.0))
        self.ledger.spent = plan.spent
        for t in parsed["tasks"]:
            if t.status == "in_progress":
                t.status = "not-started"
            elif t.blocked and retry_blocked and not t.status.startswith("blocked: safeguard"):
                t.status = "not-started"  # safeguard blocks are never auto-retried (§B.6)
        plan.set_tasks(parsed["tasks"])
        # preserve pre-existing runtime fields set_tasks() marked in-progress
        plan.status = "in-progress"

        while not plan.all_terminal():
            ready = plan.ready_tasks()
            if not ready:
                self.bus.emit(EventType.INFO,
                              "no dispatchable tasks remain (blocked dependencies) — stopping")
                break
            for task in ready:
                self._dispatch(plan, task)
                if task.agent == "architect" and task.status == "completed":
                    self._human_architecture_gate(plan, task)
        plan.status = "complete" if all(t.status == "completed" for t in plan.tasks.values()) else "review"
        plan._flush()
        return plan

    # ---------- §B.5 dispatch: same-tier retry, then walk the chain ----------
    def _dispatch(self, plan: PlanStore, task: Task) -> None:
        route_plan = self.router.plan(task, self.ledger)
        for tier_idx, cand in enumerate(route_plan.chain):
            if tier_idx > 0:
                self.bus.emit(EventType.ESCALATION,
                              f"escalating along fallback chain → {cand.backend}@{cand.effort}",
                              task_id=task.id)
            for attempt in range(1 + self.SAME_TIER_RETRIES):
                terminal = self._attempt(plan, task, cand)
                if terminal:
                    return
                if attempt < self.SAME_TIER_RETRIES:
                    self.bus.emit(EventType.FALLBACK,
                                  f"retrying once on same tier ({cand.backend}@{cand.effort})",
                                  task_id=task.id)
                    task.status = "not-started"
            task.status = "not-started"  # move to next tier
        task.status = "blocked: chain-exhausted"
        self.bus.emit(EventType.ESCALATION,
                      "fallback chain exhausted — task blocked for human review",
                      task_id=task.id)
        plan._flush()

    def _attempt(self, plan: PlanStore, task: Task, cand: Candidate) -> bool:
        """One dispatch attempt. Returns True when the task is terminal
        (completed or safeguard-blocked — never re-dispatch refused content)."""
        adapter = self.adapters[cand.backend]
        plan.assign(task.id, cand.backend, cand.effort)
        self.ledger.reserve(task, cand.backend, cand.effort)
        plan.reserved = sum(self.ledger.reserved.values())

        is_ultra = cand.backend == "gpt-5.6-sol" and cand.effort == "ultra"
        gov = self.governor.acquire() if is_ultra else None
        try:
            result = self.runner.run(
                task, adapter,
                self.builder.system_prompt(task),
                self.builder.opening_message(
                    task,
                    attachments=self.stores.attachments(task.agent),
                    recovery=self.runner.load_scratch(task)))
        finally:
            if gov:
                gov.release()

        cost = self.ledger.settle(task, cand.backend, result.tokens_in, result.tokens_out)
        plan.record_cost(task.id, cost)
        plan.reserved = sum(self.ledger.reserved.values())
        plan.merge_cache(self.stores.path("cache"), task, self.bus)
        return (task.status == "completed" or result.safeguard
                or task.status.startswith("blocked: safeguard"))

    # ---------- gates (Phase-1 human architecture gate, unchanged) ----------
    def _human_architecture_gate(self, plan: PlanStore, task: Task) -> None:
        self.bus.emit(EventType.HUMAN_REQUIRED, "Architecture approval", task_id=task.id)
        if self._checkpoint("Architecture ready (see potaga-shared). Approve to continue?"):
            self.bus.emit(EventType.GATE_PASS, "architecture approved by human", task_id=task.id)
        else:
            self.bus.emit(EventType.GATE_FAIL, "architecture approval declined — halting", task_id=task.id)
            for t in plan.tasks.values():
                if t.status == "not-started":
                    t.status = "blocked: architecture-not-approved"
