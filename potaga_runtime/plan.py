"""The plan: task model, single-writer store, decision log.

MULTI_AGENT_PLAN.md has exactly one writer — this class (policy §B.3).
Agents never touch it: the runner writes their status updates into their
potaga-cache partition, and the Orchestrator calls PlanStore.merge_cache().
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .events import Event, EventBus, EventType

AGENTS = ("architect", "coder", "tester", "reviewer", "docs", "research")
STATUSES = ("not-started", "in_progress", "completed")  # plus blocked: <reason>


@dataclass
class Task:
    id: str
    description: str
    agent: str
    input_contract: str
    output_contract: str
    scope_boundary: str
    success_criteria: str
    dependencies: List[str] = field(default_factory=list)
    security: bool = False
    est_tokens_in: int = 4000
    est_tokens_out: int = 2000
    # runtime-assigned
    model: str = ""
    effort: str = ""
    status: str = "not-started"
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    notes: str = ""

    def validate(self) -> None:
        if self.agent not in AGENTS:
            raise ValueError(f"task {self.id}: unknown agent '{self.agent}'")
        if not self.scope_boundary.strip().upper().startswith("ONLY"):
            raise ValueError(f"task {self.id}: scope boundary must start with 'ONLY'")

    @property
    def blocked(self) -> bool:
        return self.status.startswith("blocked")


class PlanStore:
    """Single writer of MULTI_AGENT_PLAN.md. Subscribes to the event bus."""

    def __init__(self, shared_dir: pathlib.Path, project: str, goal: str,
                 ceiling_usd: float, bus: EventBus) -> None:
        self.path = shared_dir / "MULTI_AGENT_PLAN.md"
        self.project, self.goal, self.ceiling = project, goal, ceiling_usd
        self.status = "planning"
        self.tasks: Dict[str, Task] = {}
        self.decision_log: List[str] = []
        self.conflict_cards: List[str] = []  # rendered ConflictCard blocks
        self.spent = 0.0
        self.reserved = 0.0
        bus.subscribe(self._on_event)
        shared_dir.mkdir(parents=True, exist_ok=True)
        self._flush()

    # ---------- event sink ----------
    def _on_event(self, ev: Event) -> None:
        self.decision_log.append(ev.render())
        self._flush()

    # ---------- mutations (Orchestrator-only call sites) ----------
    def set_tasks(self, tasks: List[Task]) -> None:
        for t in tasks:
            t.validate()
        self.tasks = {t.id: t for t in tasks}
        self.status = "in-progress"
        self._flush()

    def assign(self, task_id: str, model: str, effort: str) -> None:
        t = self.tasks[task_id]
        t.model, t.effort, t.status = model, effort, "in_progress"
        self._flush()

    def merge_cache(self, cache_dir: pathlib.Path, task: Task, bus: EventBus) -> None:
        """Read the agent's status.json from its cache partition and merge (§B.3)."""
        f = cache_dir / f"cache/{task.agent}/status_{task.id}.json"
        if not f.exists():
            task.status = "blocked: no-status-posted"
        else:
            data = json.loads(f.read_text())
            task.status = str(data.get("status", "blocked: malformed-status"))
            task.notes = str(data.get("notes", ""))[:500]
            task.tokens_in = int(data.get("tokens_in", 0))
            task.tokens_out = int(data.get("tokens_out", 0))
        if not (task.status in STATUSES or task.status.startswith("blocked")):
            task.status = f"blocked: invalid-status ({task.status[:40]})"
        bus.emit(EventType.TASK_STATUS, f"{task.agent} → {task.status}", task_id=task.id)

    def record_conflict(self, rendered_card: str) -> None:
        self.conflict_cards.append(rendered_card)
        self._flush()

    def record_cost(self, task_id: str, cost: float) -> None:
        self.tasks[task_id].cost_usd += cost
        self.spent += cost
        self._flush()

    def ready_tasks(self) -> List[Task]:
        done = {t.id for t in self.tasks.values() if t.status == "completed"}
        return [t for t in self.tasks.values()
                if t.status == "not-started" and all(d in done for d in t.dependencies)]

    def all_terminal(self) -> bool:
        return all(t.status == "completed" or t.blocked for t in self.tasks.values())

    # ---------- rendering ----------
    def _flush(self) -> None:
        lines = [
            "# MULTI_AGENT_PLAN.md",
            f"## Project: {self.project}",
            f"## Goal: {self.goal}",
            f"## Status: {self.status}",
            f"## Budget: ceiling ${self.ceiling:.2f} · spent ${self.spent:.2f} · reserved ${self.reserved:.2f}",
            "",
            "## Tasks",
        ]
        for t in self.tasks.values():
            lines += [
                f"### Task {t.id}: {t.description}",
                f"- Agent: {t.agent}",
                f"- Model/effort: {t.model}@{t.effort}" if t.model else "- Model/effort: <unassigned>",
                f"- Security: {str(t.security).lower()}",
                f"- Dependencies: [{', '.join(t.dependencies)}]",
                f"- Input contract: {t.input_contract}",
                f"- Output contract: {t.output_contract}",
                f"- Scope boundary: {t.scope_boundary}",
                f"- Success criteria: {t.success_criteria}",
                f"- Status: {t.status}",
                f"- Tokens (logical in/out): {t.est_tokens_in}/{t.est_tokens_out} · (effective billed): {t.tokens_in}/{t.tokens_out}",
                f"- Cost: ${t.cost_usd:.2f}",
                *( [f"- Notes: {t.notes}"] if t.notes else [] ),
                "",
            ]
        lines += ["## Decision Log"] + [f"- {l}" for l in self.decision_log]
        lines += ["", "## Architecture Decisions", "", "## Conflict Log", ""]
        for card in self.conflict_cards:
            lines += [card, ""]
        self.path.write_text("\n".join(lines))


# ---------- rendered-plan parsing (cross-session resume) ----------
_META_RE = re.compile(r"^## (Project|Goal|Status): (.*)$")
_BUDGET_RE = re.compile(r"ceiling \$([\d.]+) · spent \$([\d.]+)")


def parse_rendered_plan(markdown: str) -> dict:
    """Parse a plan previously rendered by PlanStore back into metadata and
    Tasks (including runtime fields: status, model/effort, cost)."""
    meta: dict = {}
    for line in markdown.splitlines():
        m = _META_RE.match(line.strip())
        if m:
            meta[m.group(1).lower()] = m.group(2)
        b = _BUDGET_RE.search(line)
        if b:
            meta["ceiling"], meta["spent"] = float(b.group(1)), float(b.group(2))
    tasks = parse_tasks(markdown)
    # re-attach runtime fields the schema parser ignores
    blocks = re.split(r"^### Task ", markdown, flags=re.M)[1:]
    for t, block in zip(tasks, blocks):
        sm = re.search(r"^- Status: (.+)$", block, re.M)
        mm = re.search(r"^- Model/effort: (.+)$", block, re.M)
        cm = re.search(r"^- Cost: \$([\d.]+)$", block, re.M)
        if sm:
            t.status = sm.group(1).strip()
        if mm and "@" in mm.group(1):
            t.model, _, t.effort = mm.group(1).strip().partition("@")
        if cm:
            t.cost_usd = float(cm.group(1))
    return {"meta": meta, "tasks": tasks}


# ---------- decomposition parsing ----------
_TASK_RE = re.compile(r"^### Task ([\w.-]+): (.+)$")
_FIELD_RE = re.compile(r"^- ([A-Za-z ()/]+): (.*)$")


def parse_tasks(markdown: str) -> List[Task]:
    """Parse the decomposer's output (plan Tasks schema) into Task objects."""
    tasks: List[Task] = []
    cur: Optional[dict] = None

    def close() -> None:
        nonlocal cur
        if cur is None:
            return
        deps = [d.strip() for d in cur.get("dependencies", "").strip("[]").split(",") if d.strip()]
        est = re.findall(r"\d+", cur.get("tokens", "")) or ["4000", "2000"]
        tasks.append(Task(
            id=cur["id"], description=cur["description"],
            agent=cur.get("agent", "").strip().lower(),
            input_contract=cur.get("input contract", ""),
            output_contract=cur.get("output contract", ""),
            scope_boundary=cur.get("scope boundary", ""),
            success_criteria=cur.get("success criteria", ""),
            dependencies=deps,
            security=cur.get("security", "false").strip().lower() == "true",
            est_tokens_in=int(est[0]), est_tokens_out=int(est[-1]),
        ))
        cur = None

    for raw in markdown.splitlines():
        line = raw.strip()
        m = _TASK_RE.match(line)
        if m:
            close()
            cur = {"id": m.group(1), "description": m.group(2)}
            continue
        if cur is None:
            continue
        f = _FIELD_RE.match(line)
        if f:
            key = f.group(1).strip().lower()
            if key.startswith("tokens"):
                key = "tokens"
            cur[key] = f.group(2).strip()
    close()
    if not tasks:
        raise ValueError("decomposer output contained no tasks in the plan schema")
    return tasks
