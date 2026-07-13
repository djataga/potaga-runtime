"""Backend adapters.

An adapter translates (model, effort) into one provider call and returns the
agent's turns. Two implementations ship with Phase 1:

- MockAdapter: deterministic, offline; drives tests and --dry-run.
- AnthropicAdapter: real Sonnet calls. The model ID comes from runtime config
  (default 'claude-sonnet-5' per the spec); it is NOT hardcoded truth about
  what your API deployment offers — set `model_ids` to a model string your
  account can access, and enable `supports_effort_param` only if your
  deployment accepts an `effort` parameter. When disabled, effort is folded
  into the system prompt so the adapter degrades instead of erroring.
  Verify current model names and parameters at https://docs.claude.com.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Protocol


@dataclass
class ToolCall:
    name: str
    args: Dict
    id: str = ""


@dataclass
class Turn:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    stop_reason: str = "end_turn"
    safeguard_refusal: bool = False


class Adapter(Protocol):
    backend: str

    def run_turn(self, system: str, messages: List[Dict], tools: List[Dict],
                 effort: str, max_tokens: int) -> Turn: ...


# ---------------------------------------------------------------- mock
class MockAdapter:
    """Offline adapter: 'completes' every task by saving a stub artifact and
    posting a completed status — exercising the full dispatch path."""

    backend = "sonnet-5"

    def run_turn(self, system: str, messages: List[Dict], tools: List[Dict],
                 effort: str, max_tokens: int) -> Turn:
        asked_tools = {t["name"] for t in tools}
        last = messages[-1]["content"] if messages else ""
        already_saved = any(
            isinstance(m.get("content"), list) and
            any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
            for m in messages
        )
        if "DECOMPOSE" in str(last):
            return Turn(text=_MOCK_PLAN, tokens_in=1200, tokens_out=600)
        if not already_saved and "save_output" in asked_tools:
            return Turn(tool_calls=[
                ToolCall("save_output", {"relpath": "artifact.md",
                                         "content": f"# mock artifact\n(effort={effort})"}, id="t1"),
                ToolCall("post_status", {"status": "completed",
                                         "notes": "mock run — artifact saved"}, id="t2"),
            ], tokens_in=900, tokens_out=180, stop_reason="tool_use")
        return Turn(text="Done.", tokens_in=200, tokens_out=20)


_MOCK_PLAN = """### Task 1: Define architecture and contracts
- Agent: architect
- Security: false
- Dependencies: []
- Input contract: verbatim user request
- Output contract: contracts to potaga-shared
- Scope boundary: ONLY the architecture for the stated request
- Success criteria: human approval gate passed
- Tokens (logical in/out): 4000/1500

### Task 2: Implement core module
- Agent: coder
- Security: true
- Dependencies: [1]
- Input contract: contracts from Task 1
- Output contract: source to potaga-code
- Scope boundary: ONLY the module specified in the contract
- Success criteria: sandbox-verified
- Tokens (logical in/out): 6000/3000

### Task 3: Write tests
- Agent: tester
- Security: false
- Dependencies: [2]
- Input contract: source from Task 2
- Output contract: tests and coverage to potaga-tests
- Scope boundary: ONLY tests for the contracted module
- Success criteria: coverage >= 70%
- Tokens (logical in/out): 4000/2000
"""


# ------------------------------------------------------------ anthropic
class AnthropicAdapter:
    backend = "sonnet-5"

    def __init__(self, model_id: str, supports_effort_param: bool = False) -> None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set — use --dry-run for the mock adapter")
        import anthropic  # deferred so the mock path has no hard dependency
        self._client = anthropic.Anthropic()
        self._model = model_id
        self._effort_param = supports_effort_param

    def run_turn(self, system: str, messages: List[Dict], tools: List[Dict],
                 effort: str, max_tokens: int) -> Turn:
        kwargs = dict(model=self._model, max_tokens=max_tokens,
                      system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if self._effort_param:
            kwargs["effort"] = effort
        else:
            kwargs["system"] = f"{system}\n\n<effort>{effort}</effort>"
        resp = self._client.messages.create(**kwargs)
        turn = Turn(tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason or "end_turn")
        for block in resp.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                turn.tool_calls.append(ToolCall(block.name, dict(block.input), id=block.id))
        if resp.stop_reason == "refusal":
            turn.safeguard_refusal = True
        return turn


def serialize_tool_results(results: List[Dict]) -> Dict:
    """Build the user message carrying tool results back to the model."""
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r["id"], "content": json.dumps(r["result"])}
        for r in results
    ]}
