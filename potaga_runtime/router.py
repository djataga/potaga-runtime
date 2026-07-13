"""Router and budget ledger.

Phase 1 router: loads the full routing matrix and walks each route's chain,
skipping backends whose status is not 'available' (the Availability Monitor
hook), but only the Anthropic adapter is registered — so in practice every
subtask lands on sonnet-5. The CQP scoring slot is stubbed for Phase 3.

Ledger implements policy §B.2: reserve at dispatch, effective tokens =
logical × loop multiplier × Ultra multiplier × tokenizer factor; 80% soft /
90% hard thresholds; pricing epoch via Config.pricing_for().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from .config import Config
from .events import EventBus, EventType
from .plan import Task

AGENT_ROUTE = {  # Phase-1 mapping from agent to a default route type
    "architect": "architecture",
    "coder": "backend_coding",
    "tester": "test_generation",
    "reviewer": "security_review",
    "docs": "documentation",
    "research": "research",
}


@dataclass(frozen=True)
class Assignment:
    backend: str
    effort: str
    route: str
    degraded: bool


class AvailabilityMonitor:
    """Backend status source. Phase 1: static — GA backends with a registered
    adapter are 'available'; everything else is 'unavailable'."""

    def __init__(self, config: Config, registered_backends: set[str]) -> None:
        self._config = config
        self._registered = registered_backends

    def status(self, backend: str) -> str:
        cfg = self._config.matrix["backends"].get(backend, {})
        if backend in self._registered and cfg.get("ga", False):
            return "available"
        return "unavailable"


class Router:
    def __init__(self, config: Config, monitor: AvailabilityMonitor, bus: EventBus) -> None:
        self.config, self.monitor, self.bus = config, monitor, bus

    def route(self, task: Task) -> Assignment:
        route_name = AGENT_ROUTE[task.agent]
        route = self.config.matrix["routes"][route_name]
        chain = [route["primary"]] + route.get("fallbacks", [])
        floor = route.get("floor")
        for i, entry in enumerate(chain):
            backend, _, effort = str(entry).partition("@")
            if self.monitor.status(backend) != "available":
                continue
            if task.security and floor and not self._at_or_above_floor(backend, floor, chain):
                continue
            degraded = i > 0
            ev = EventType.DEGRADED_MODE if degraded else EventType.ROUTING
            self.bus.emit(ev, f"{route_name} → {backend}@{effort}"
                          + (f" (primary {chain[0]} unavailable)" if degraded else ""),
                          task_id=task.id)
            return Assignment(backend, effort, route_name, degraded)
        raise RuntimeError(f"no available backend for route '{route_name}' "
                           f"(chain: {chain}) — check adapters/Availability Monitor")

    @staticmethod
    def _at_or_above_floor(backend: str, floor: str, chain: list) -> bool:
        """Security floor (§B.1): a backend qualifies iff it is the floor or
        sits before the floor in the declared chain."""
        order = [str(e).partition("@")[0] for e in chain]
        if floor not in order:
            return True
        return order.index(backend) <= order.index(floor)


class BudgetLedger:
    def __init__(self, config: Config, ceiling_usd: float, bus: EventBus,
                 confirm: Callable[[str], bool]) -> None:
        self.config, self.ceiling, self.bus = config, ceiling_usd, bus
        self.spent = 0.0
        self.reserved: Dict[str, float] = {}
        self._confirm = confirm  # human hook for the 90% hard pause
        self._soft_warned = False

    # ---------- policy §B.2 ----------
    def effective_tokens(self, logical: int, backend: str, effort: str,
                         content: str = "code") -> int:
        d = self.config.defaults
        mult = float(d.get("loop_multiplier", 10))
        if backend == "gpt-5.6-sol" and effort == "ultra":
            mult *= float(d.get("ultra_multiplier", 3.5))
        tok = float(self.config.parameters.get("tokenizer_factors", {}).get(content, 1.1))
        return int(logical * mult * tok)

    def estimate_cost(self, task: Task, backend: str, effort: str) -> float:
        price = self.config.pricing_for(backend)
        ein = self.effective_tokens(task.est_tokens_in, backend, effort)
        eout = self.effective_tokens(task.est_tokens_out, backend, effort)
        return ein / 1e6 * price["input_per_m"] + eout / 1e6 * price["output_per_m"]

    def reserve(self, task: Task, backend: str, effort: str) -> None:
        est = self.estimate_cost(task, backend, effort)
        projected = self.spent + sum(self.reserved.values()) + est
        b = self.config.budget_thresholds
        if projected >= self.ceiling * float(b["hard_pause_pct"]):
            self.bus.emit(EventType.BUDGET,
                          f"projected ${projected:.2f} ≥ {b['hard_pause_pct']:.0%} of ceiling — pausing for user",
                          task_id=task.id)
            if not self._confirm(f"Budget at {projected / self.ceiling:.0%} of ceiling "
                                 f"(${projected:.2f}/${self.ceiling:.2f}). Continue?"):
                raise BudgetExceeded(task.id, projected, self.ceiling)
        elif projected >= self.ceiling * float(b["soft_warning_pct"]) and not self._soft_warned:
            self._soft_warned = True
            self.bus.emit(EventType.BUDGET,
                          f"projected ${projected:.2f} ≥ {b['soft_warning_pct']:.0%} of ceiling — "
                          "prefer cost-efficient backends for non-critical tasks")
        self.reserved[task.id] = est

    def settle(self, task: Task, backend: str, tokens_in: int, tokens_out: int) -> float:
        price = self.config.pricing_for(backend)
        cost = tokens_in / 1e6 * price["input_per_m"] + tokens_out / 1e6 * price["output_per_m"]
        self.spent += cost
        self.reserved.pop(task.id, None)
        return cost


class BudgetExceeded(RuntimeError):
    def __init__(self, task_id: str, projected: float, ceiling: float) -> None:
        super().__init__(f"budget hard-pause declined at task {task_id}: "
                         f"projected ${projected:.2f} vs ceiling ${ceiling:.2f}")
