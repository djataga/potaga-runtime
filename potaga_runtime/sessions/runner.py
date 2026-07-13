"""Session builder + agent runner.

Builder: preamble + role prompt + injected subtask contract, exactly as the
repo README specifies. Runner: the turn loop — executes the two Phase-1
tools (save_output, post_status) against the memory stores through the
agent's grant, enforces the per-backend timeout, and converts platform
refusals into `blocked: safeguard` without ever re-dispatching (policy §B.6).
"""
from __future__ import annotations

import json
import pathlib
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List

from ..config import Config
from ..events import EventBus, EventType
from ..memory import MemoryStores, StoreGrant
from ..plan import Task
from .adapters.core import Adapter, Turn, serialize_tool_results

PROMPT_FILES = {
    "architect": "01_architect.md", "coder": "02_coder.md", "tester": "03_tester.md",
    "reviewer": "04_reviewer.md", "docs": "05_docs.md", "research": "06_research.md",
}

TOOLS = [
    {
        "name": "save_output",
        "description": "Save a deliverable file to your agent's writable memory store.",
        "input_schema": {"type": "object", "properties": {
            "relpath": {"type": "string"}, "content": {"type": "string"}},
            "required": ["relpath", "content"]},
    },
    {
        "name": "post_status",
        "description": "Post your final subtask status to your potaga-cache partition. "
                       "status must be 'completed' or 'blocked: <reason>'.",
        "input_schema": {"type": "object", "properties": {
            "status": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["status"]},
    },
]

PRIMARY_STORE = {"architect": "shared", "coder": "code", "tester": "tests",
                 "reviewer": "reviews", "docs": "docs", "research": "research"}


class SessionBuilder:
    def __init__(self, config: Config) -> None:
        prompts = config.repo / "prompts"
        self._preamble = (prompts / "00_shared_preamble.md").read_text()
        self._roles = {a: (prompts / f).read_text() for a, f in PROMPT_FILES.items()}

    def system_prompt(self, task: Task) -> str:
        return f"{self._preamble}\n\n{self._roles[task.agent]}"

    @staticmethod
    def opening_message(task: Task, attachments: list | None = None,
                        recovery: str | None = None) -> str:
        mounts = ""
        if attachments:
            mounts = "\n\nMounted memory stores (fixed at session creation):\n" + "\n".join(
                f"- {a['store']} [{a['access']}] — {a['instructions']}" for a in attachments)
        rec = ""
        if recovery:
            rec = ("\n\nRECOVERY PROTOCOL: a previous session for this subtask saved state "
                   "before its context was reset. Read it, read the plan, read the relevant "
                   f"stores, then resume — do not restart from scratch.\nSaved state:\n{recovery}")
        return (
            f"## Subtask {task.id}: {task.description}\n"
            f"- Input contract: {task.input_contract}\n"
            f"- Output contract: {task.output_contract}\n"
            f"- Scope boundary: {task.scope_boundary}\n"
            f"- Success criteria: {task.success_criteria}\n"
            f"- Security: {task.security}\n\n"
            "Phase-1 runtime tools available: save_output (writes to your primary store) "
            "and post_status (writes to your cache partition; the Orchestrator merges it "
            "into the plan). Complete the subtask, save your deliverable, then post exactly "
            "one final status."
        ) + mounts + rec


@dataclass
class RunResult:
    tokens_in: int
    tokens_out: int
    safeguard: bool


def _estimate_tokens(messages: List[Dict]) -> int:
    return sum(len(json.dumps(m)) for m in messages) // 4


class AgentRunner:
    MAX_TURNS = 8
    # context-eviction threshold (logical tokens); operator-tunable
    CONTEXT_LIMIT = 24000

    def __init__(self, stores: MemoryStores, bus: EventBus, config: Config,
                 context_limit: int | None = None) -> None:
        self.stores, self.bus, self.config = stores, bus, config
        if context_limit:
            self.CONTEXT_LIMIT = context_limit

    def run(self, task: Task, adapter: Adapter, system: str, opening: str) -> RunResult:
        grant: StoreGrant = self.stores.grant(task.agent)
        session_id = uuid.uuid4().hex[:8]
        deadline = time.monotonic() + self.config.timeout_for(adapter.backend)
        messages: List[Dict] = [{"role": "user", "content": opening}]
        tin = tout = 0
        status_posted = False

        for _ in range(self.MAX_TURNS):
            if time.monotonic() > deadline:
                self._post_status(grant, task, session_id,
                                  {"status": "blocked: timeout", "notes": "runner deadline hit"})
                self.bus.emit(EventType.TASK_STATUS, "timeout — preempted", task_id=task.id)
                return RunResult(tin, tout, safeguard=False)

            if _estimate_tokens(messages) > self.CONTEXT_LIMIT:
                messages = self._evict(grant, task, session_id, messages, tin, tout)

            turn: Turn = adapter.run_turn(system, messages, TOOLS,
                                          effort=task.effort, max_tokens=4096)
            tin += turn.tokens_in
            tout += turn.tokens_out

            if turn.safeguard_refusal:
                # §B.6: record verbatim, block, never re-dispatch the same content
                self._post_status(grant, task, session_id, {
                    "status": "blocked: safeguard",
                    "notes": f"platform refusal (verbatim): {turn.text[:300]}",
                    "tokens_in": tin, "tokens_out": tout})
                self.bus.emit(EventType.SAFEGUARD,
                              "platform declined — recorded verbatim, not re-dispatched",
                              task_id=task.id)
                return RunResult(tin, tout, safeguard=True)

            if not turn.tool_calls:
                break  # model finished talking without tools

            results = []
            assistant_content = ([{"type": "text", "text": turn.text}] if turn.text else []) + [
                {"type": "tool_use", "id": tc.id or f"tc{i}", "name": tc.name, "input": tc.args}
                for i, tc in enumerate(turn.tool_calls)]
            messages.append({"role": "assistant", "content": assistant_content})

            for i, tc in enumerate(turn.tool_calls):
                tc.id = tc.id or f"tc{i}"
                if tc.name == "save_output":
                    path = self.stores.write(
                        grant, PRIMARY_STORE[task.agent],
                        f"task_{task.id}/{tc.args['relpath']}", tc.args["content"],
                        model=f"{task.model}@{task.effort}", session_id=session_id)
                    results.append({"id": tc.id, "result": {"saved": str(path.name)}})
                elif tc.name == "post_status":
                    self._post_status(grant, task, session_id, {
                        **tc.args, "tokens_in": tin, "tokens_out": tout})
                    status_posted = True
                    results.append({"id": tc.id, "result": {"posted": True}})
                else:
                    # tool allowlist: anything else is blocked before execution
                    results.append({"id": tc.id,
                                    "result": {"error": f"tool '{tc.name}' not in allowlist"}})
                    self.bus.emit(EventType.INFO, f"blocked out-of-allowlist tool '{tc.name}'",
                                  task_id=task.id)
            messages.append(serialize_tool_results(results))
            if status_posted:
                break

        if not status_posted:
            self._post_status(grant, task, session_id, {
                "status": "blocked: no-status-posted",
                "notes": "agent ended without post_status",
                "tokens_in": tin, "tokens_out": tout})
        return RunResult(tin, tout, safeguard=False)

    def _evict(self, grant: StoreGrant, task: Task, session_id: str,
               messages: List[Dict], tin: int, tout: int) -> List[Dict]:
        """Context eviction (spec §9.6): save working state to the cache
        partition FIRST, then compact — keep the opening message and the two
        most recent exchanges; evict intermediate tool traffic."""
        scratch = {
            "task_id": task.id, "turns_so_far": len(messages),
            "tokens": {"in": tin, "out": tout},
            "note": "context compacted; intermediate tool outputs evicted "
                    "(architectural decisions and deliverables live in the stores)",
        }
        self.stores.write(grant, "cache", f"scratch_{task.id}.json",
                          json.dumps(scratch, indent=2),
                          model=f"{task.model}@{task.effort}", session_id=session_id)
        self.bus.emit(EventType.INFO,
                      "context limit approached — scratch saved, history compacted",
                      task_id=task.id)
        compacted = [messages[0]]
        compacted.append({"role": "user", "content":
                          "[runtime notice] Earlier turns were evicted to stay within the "
                          "context limit. Your working state is saved in your cache "
                          "partition; deliverables already saved to stores are safe."})
        compacted.extend(messages[-2:])
        return compacted

    def load_scratch(self, task: Task) -> str | None:
        rel = f"cache/{task.agent}/scratch_{task.id}.json"
        return self.stores.read("cache", rel) if self.stores.exists("cache", rel) else None

    def _post_status(self, grant: StoreGrant, task: Task, session_id: str, payload: Dict) -> None:
        payload.setdefault("tokens_in", 0)
        payload.setdefault("tokens_out", 0)
        self.stores.write(grant, "cache", f"status_{task.id}.json",
                          json.dumps(payload, indent=2),
                          model=f"{task.model}@{task.effort}", session_id=session_id)
