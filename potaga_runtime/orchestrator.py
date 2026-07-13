"""Decomposer + Orchestrator.

Decomposer: the ONE place the control plane calls an LLM, using the §A
prompt from prompts/07_orchestrator.md verbatim, output validated against
the plan schema.

Orchestrator: the deterministic loop — decompose → route → reserve →
dispatch → merge → gates → deliver. Error recovery per §B.5: one retry on
the same tier, then walk the fallback chain, all logged.
"""
from __future__ import annotations

import pathlib
import re
from typing import Callable, Dict, List

from .config import Config
from .events import EventBus, EventType
from .memory import MemoryStores
from .plan import PlanStore, Task, parse_tasks
from .router import AvailabilityMonitor, BudgetLedger, Router
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
    def __init__(self, config: Config, workspace: pathlib.Path, adapters: Dict[str, Adapter],
                 bus: EventBus, ceiling_usd: float,
                 confirm: Callable[[str], bool], checkpoint: Callable[[str], bool]) -> None:
        self.config, self.bus = config, bus
        self.stores = MemoryStores(workspace)
        self.adapters = adapters
        self.monitor = AvailabilityMonitor(config, set(adapters))
        self.router = Router(config, self.monitor, bus)
        self.ledger = BudgetLedger(config, ceiling_usd, bus, confirm)
        self.builder = SessionBuilder(config)
        self.runner = AgentRunner(self.stores, bus, config)
        self.ceiling = ceiling_usd
        self._checkpoint = checkpoint  # human gate hook

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
            for task in ready:  # Phase 1: sequential; §B parallelism arrives in Phase 3
                self._dispatch(plan, task)
                if task.agent == "architect" and task.status == "completed":
                    self._human_architecture_gate(plan, task)

        plan.status = "complete" if all(t.status == "completed" for t in plan.tasks.values()) else "review"
        plan._flush()
        return plan

    # ---------- dispatch with §B.5 error recovery ----------
    def _dispatch(self, plan: PlanStore, task: Task) -> None:
        assignment = self.router.route(task)
        for attempt, (backend, effort) in enumerate(
                [(assignment.backend, assignment.effort), (assignment.backend, assignment.effort)]):
            adapter = self.adapters[backend]
            plan.assign(task.id, backend, effort)
            self.ledger.reserve(task, backend, effort)
            plan.reserved = sum(self.ledger.reserved.values())
            result = self.runner.run(task, adapter,
                                     self.builder.system_prompt(task),
                                     self.builder.opening_message(task))
            cost = self.ledger.settle(task, backend, result.tokens_in, result.tokens_out)
            plan.record_cost(task.id, cost)
            plan.reserved = sum(self.ledger.reserved.values())
            plan.merge_cache(self.stores.path("cache"), task, self.bus)

            if task.status == "completed" or result.safeguard or task.status.startswith("blocked: safeguard"):
                return  # done, or §B.6: never re-dispatch refused content
            if attempt == 0:
                self.bus.emit(EventType.FALLBACK,
                              f"retrying once on same tier ({backend}@{effort})", task_id=task.id)
                task.status = "not-started"
        # both attempts failed on the primary tier — Phase 3 walks the fallback chain here
        self.bus.emit(EventType.ESCALATION,
                      "retry exhausted on primary tier; leaving blocked for escalation",
                      task_id=task.id)

    # ---------- gates (Phase-1: the mandatory human architecture gate) ----------
    def _human_architecture_gate(self, plan: PlanStore, task: Task) -> None:
        self.bus.emit(EventType.HUMAN_REQUIRED, "Architecture approval", task_id=task.id)
        if self._checkpoint("Architecture ready (see potaga-shared). Approve to continue?"):
            self.bus.emit(EventType.GATE_PASS, "architecture approved by human", task_id=task.id)
        else:
            self.bus.emit(EventType.GATE_FAIL, "architecture approval declined — halting", task_id=task.id)
            for t in plan.tasks.values():
                if t.status == "not-started":
                    t.status = "blocked: architecture-not-approved"
