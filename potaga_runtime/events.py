"""Typed events and the event bus.

Every module emits events here; the PlanStore subscribes and persists them
into the Decision Log. This is how "no silent behavior" is enforced
architecturally (spec §2.4, orchestrator policy).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List


class EventType(str, Enum):
    ROUTING = "routing"
    FALLBACK = "fallback"
    DEGRADED_MODE = "degraded-mode"
    GATE_PASS = "gate-pass"
    GATE_FAIL = "gate-fail"
    SAFEGUARD = "safeguard-refusal"
    CONFLICT = "conflict"
    ESCALATION = "escalation"
    HUMAN_REQUIRED = "human-required"
    BUDGET = "budget"
    TASK_STATUS = "task-status"
    INFO = "info"


@dataclass(frozen=True)
class Event:
    type: EventType
    detail: str
    task_id: str | None = None
    ts: str = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))

    def render(self) -> str:
        scope = f" [task {self.task_id}]" if self.task_id else ""
        return f"[{self.ts}] {self.type.value}{scope} — {self.detail}"


class EventBus:
    """Synchronous fan-out bus. One sink is mandatory: the PlanStore."""

    def __init__(self) -> None:
        self._subscribers: List[Callable[[Event], None]] = []
        self.history: List[Event] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def emit(self, type: EventType, detail: str, task_id: str | None = None) -> Event:
        ev = Event(type=type, detail=detail, task_id=task_id)
        self.history.append(ev)
        for fn in self._subscribers:
            fn(ev)
        return ev
