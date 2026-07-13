"""Router and budget ledger — Phase 3.

Router implements the five-stage pipeline from spec §4.4:
  1. classify the subtask into a route type (agent + content hints)
  2. availability filter (Availability Monitor; non-GA backends skipped when down)
  3. quality-threshold elimination (default: 80% of the best available quality)
  4. CQP scoring — quality per dollar — with the cqp_margin tie-break and the
     cost-ceiling preference (≥80% budget → non-critical tasks optimize cost)
  5. fallback assignment: the remaining qualifying candidates, declared order

Containment for Sol Ultra (spec §5, matrix special_rules): calls serialized
and capped per project; when the cap is exhausted, sol-ultra entries are
skipped as if unavailable.

BudgetLedger implements policy §B.2 (reserve at dispatch, multipliers,
80/90% thresholds) and §B.10 (pricing epoch) — unchanged from Phase 1.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from .config import Config
from .events import EventBus, EventType
from .plan import Task

AGENT_ROUTE = {
    "architect": "architecture",
    "coder": "backend_coding",
    "tester": "test_generation",
    "reviewer": "security_review",
    "docs": "documentation",
    "research": "research",
}

_UI_HINTS = ("ui", "frontend", "front-end", "css", "html", "landing page", "component", "design")

# Quality proxies (0–100) per route type, derived from the verified benchmark
# reference in spec §4.1 (SWE-Bench, Terminal-Bench, Design Arena, BenchLM).
# Operator-tunable via runtime_overrides["quality_scores"].
DEFAULT_QUALITY: Dict[str, Dict[str, float]] = {
    "backend_coding":  {"sonnet-5": 85, "opus-4-8": 88, "glm-5.2": 70, "gpt-5.6-sol": 78},
    "hardest_coding":  {"opus-4-8": 90, "gpt-5.6-sol": 88, "sonnet-5": 82},
    "architecture":    {"sonnet-5": 88, "opus-4-8": 90},
    "deep_debugging":  {"gpt-5.6-sol": 92, "opus-4-8": 87, "sonnet-5": 80},
    "security_review": {"gpt-5.6-sol": 93, "opus-4-8": 88, "sonnet-5": 78},
    "frontend_ui":     {"glm-5.2": 92, "sonnet-5": 80},
    "documentation":   {"sonnet-5": 88, "glm-5.2": 75},
    "research":        {"gpt-5.6-terra": 88, "gpt-5.6-sol": 90, "sonnet-5": 82},
    "extraction":      {"glm-5.2": 85},
    "math_algorithms": {"sonnet-5": 88, "opus-4-8": 90, "glm-5.2": 68},
    "test_generation": {"sonnet-5": 85, "glm-5.2": 72},
    "refactoring":     {"sonnet-5": 86, "opus-4-8": 88},
    "devops_config":   {"sonnet-5": 84, "glm-5.2": 74},
    "long_horizon":    {"sonnet-5": 88, "opus-4-8": 89},
}


@dataclass(frozen=True)
class Candidate:
    backend: str
    effort: str
    quality: float
    est_cost: float

    @property
    def cqp(self) -> float:
        return self.quality / max(self.est_cost, 1e-6)


@dataclass(frozen=True)
class RoutePlan:
    route: str
    chain: List[Candidate]        # active primary first, then fallbacks in order
    degraded: bool                # declared primary was skipped

    @property
    def primary(self) -> Candidate:
        return self.chain[0]


class AvailabilityMonitor:
    """Backend status source with transition notifications (policy §B.9).

    Phase 3: statuses are dynamic — set_status() is the hook a real poller
    (or the console's simulator) calls. A backend is available iff its status
    is 'available' AND an adapter is registered. Each down-transition is
    announced once per session.
    """

    def __init__(self, config: Config, registered: set[str], bus: EventBus) -> None:
        self._config, self._registered, self._bus = config, registered, bus
        self._status: Dict[str, str] = {}
        for b, c in config.matrix["backends"].items():
            self._status[b] = "available" if (b in registered and c.get("ga", False)) else "unavailable"
        self._announced: set[str] = set()

    def set_status(self, backend: str, status: str) -> None:
        prev = self._status.get(backend)
        self._status[backend] = status
        if status != "available" and prev == "available" and backend not in self._announced:
            self._announced.add(backend)
            self._bus.emit(EventType.DEGRADED_MODE,
                           f"{backend} status → {status}; routing table swapped to GA fallbacks "
                           "(notified once per session)")

    def available(self, backend: str) -> bool:
        return backend in self._registered and self._status.get(backend) == "available"


class SolUltraGovernor:
    """Serialization + per-project call cap for sol-ultra (matrix special_rules)."""

    def __init__(self, config: Config) -> None:
        rules = config.matrix.get("special_rules", {})
        self.max_calls = int(rules.get("sol_ultra_max_calls_per_project", 6))
        self.serialized = bool(rules.get("sol_ultra_serialized", True))
        self.calls = 0
        self._lock = threading.Lock()

    def admissible(self) -> bool:
        return self.calls < self.max_calls

    def acquire(self) -> "SolUltraGovernor":
        if self.serialized:
            self._lock.acquire()
        self.calls += 1
        return self

    def release(self) -> None:
        if self.serialized and self._lock.locked():
            self._lock.release()


class Router:
    def __init__(self, config: Config, monitor: AvailabilityMonitor, bus: EventBus,
                 governor: SolUltraGovernor,
                 budget_pressure: Callable[[], float] = lambda: 0.0,
                 quality_scores: Dict[str, Dict[str, float]] | None = None) -> None:
        self.config, self.monitor, self.bus, self.governor = config, monitor, bus, governor
        self._pressure = budget_pressure  # spent+reserved as a fraction of ceiling
        self._quality = quality_scores or DEFAULT_QUALITY
        d = config.defaults
        self.threshold_pct = float(d.get("quality_threshold_pct", 80)) / 100.0
        self.cqp_margin = float(d.get("cqp_margin", 0.15))

    # ---------- stage 1: classify ----------
    def classify(self, task: Task) -> str:
        if task.agent == "coder":
            text = f"{task.description} {task.scope_boundary}".lower()
            if any(h in text for h in _UI_HINTS):
                return "frontend_ui"
        return AGENT_ROUTE[task.agent]

    # ---------- stages 2–5 ----------
    def plan(self, task: Task, ledger: "BudgetLedger") -> RoutePlan:
        route_name = self.classify(task)
        route = self.config.matrix["routes"][route_name]
        declared = [str(e) for e in [route["primary"]] + route.get("fallbacks", [])]
        floor = route.get("floor")

        # stage 2: availability + containment + security floor
        candidates: List[Candidate] = []
        for entry in declared:
            backend, _, effort = entry.partition("@")
            if not self.monitor.available(backend):
                continue
            if backend == "gpt-5.6-sol" and effort == "ultra" and not self.governor.admissible():
                self.bus.emit(EventType.INFO,
                              f"sol-ultra call cap reached ({self.governor.max_calls}) — skipping",
                              task_id=task.id)
                continue
            if task.security and floor and not self._at_or_above_floor(backend, floor, declared):
                continue
            candidates.append(Candidate(
                backend=backend, effort=effort,
                quality=self._quality.get(route_name, {}).get(backend, 60.0),
                est_cost=ledger.estimate_cost(task, backend, effort)))
        if not candidates:
            raise RuntimeError(f"no available backend for route '{route_name}' "
                               f"(declared: {declared}) — check adapters/Availability Monitor")

        # stage 3: quality threshold (relative to best available)
        best_q = max(c.quality for c in candidates)
        qualifying = [c for c in candidates if c.quality >= best_q * self.threshold_pct] or candidates

        # stage 4: CQP with the security-critical and cost-ceiling special rules (§4.5)
        mode = "balanced"
        if task.security:
            # Security-Critical Path: highest quality regardless of CQP score
            chosen = max(qualifying, key=lambda c: c.quality)
        else:
            mode = "cost" if self._pressure() >= float(
                self.config.budget_thresholds["soft_warning_pct"]) else "balanced"
            ranked = sorted(qualifying, key=lambda c: c.cqp, reverse=True)
            chosen = ranked[0]
            if len(ranked) > 1:
                top, runner = ranked[0], ranked[1]
                if (top.cqp - runner.cqp) / top.cqp < self.cqp_margin:
                    chosen = min((top, runner), key=lambda c: c.est_cost) if mode == "cost" \
                        else max((top, runner), key=lambda c: c.quality)
        if mode == "cost" and chosen is not ranked[0]:
            self.bus.emit(EventType.BUDGET,
                          f"cost-ceiling preference applied → {chosen.backend}@{chosen.effort}",
                          task_id=task.id)

        # stage 5: fallback assignment — remaining candidates in declared order
        rest = [c for c in candidates if c is not chosen]
        rest.sort(key=lambda c: declared.index(f"{c.backend}@{c.effort}"))
        degraded = f"{chosen.backend}@{chosen.effort}" != declared[0] and \
            not self.monitor.available(declared[0].partition("@")[0])
        ev = EventType.DEGRADED_MODE if degraded else EventType.ROUTING
        self.bus.emit(ev, f"{route_name} → {chosen.backend}@{chosen.effort}"
                      + (f" (declared primary {declared[0]} unavailable)" if degraded else "")
                      + f" · CQP {chosen.cqp:.0f} · fallbacks: "
                      + (", ".join(f"{c.backend}@{c.effort}" for c in rest) or "none"),
                      task_id=task.id)
        return RoutePlan(route=route_name, chain=[chosen] + rest, degraded=degraded)

    @staticmethod
    def _at_or_above_floor(backend: str, floor: str, declared: List[str]) -> bool:
        order = [e.partition("@")[0] for e in declared]
        if floor not in order:
            return True
        return order.index(backend) <= order.index(floor)


class BudgetLedger:
    def __init__(self, config: Config, ceiling_usd: float, bus: EventBus,
                 confirm: Callable[[str], bool]) -> None:
        self.config, self.ceiling, self.bus = config, ceiling_usd, bus
        self.spent = 0.0
        self.spent_by_backend: Dict[str, float] = {}
        self.reserved: Dict[str, float] = {}
        self._confirm = confirm
        self._soft_warned = False

    def pressure(self) -> float:
        return (self.spent + sum(self.reserved.values())) / self.ceiling if self.ceiling else 0.0

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
                          "cost-optimizing non-critical routing")
        self.reserved[task.id] = est

    def settle(self, task: Task, backend: str, tokens_in: int, tokens_out: int) -> float:
        price = self.config.pricing_for(backend)
        cost = tokens_in / 1e6 * price["input_per_m"] + tokens_out / 1e6 * price["output_per_m"]
        self.spent += cost
        self.spent_by_backend[backend] = self.spent_by_backend.get(backend, 0.0) + cost
        self.reserved.pop(task.id, None)
        return cost


class BudgetExceeded(RuntimeError):
    def __init__(self, task_id: str, projected: float, ceiling: float) -> None:
        super().__init__(f"budget hard-pause declined at task {task_id}: "
                         f"projected ${projected:.2f} vs ceiling ${ceiling:.2f}")
