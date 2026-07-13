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
    """Offline adapter, backend-parametric. Default behavior: 'complete' every
    task by saving a stub artifact and posting a completed status. A `script`
    of per-dispatch behaviors ('complete' | 'block' | 'refuse') lets tests
    exercise same-tier retries, fallback-chain walks, and safeguard handling.
    """

    def __init__(self, backend: str = "sonnet-5", script: List[str] | None = None) -> None:
        self.backend = backend
        self._script = list(script or [])
        self._current = "complete"

    def _behavior(self, messages: List[Dict]) -> str:
        # a new dispatch starts with exactly one (opening) message: consume one
        # script entry per dispatch; 'complete' forever once the script is empty
        if len(messages) == 1:
            self._current = self._script.pop(0) if self._script else "complete"
        return self._current

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
        behavior = self._behavior(messages)
        if behavior == "refuse":
            return Turn(text="I can't help with reproducing that exploit.",
                        tokens_in=300, tokens_out=30, stop_reason="refusal",
                        safeguard_refusal=True)
        if not already_saved and "save_output" in asked_tools:
            if behavior == "block":
                return Turn(tool_calls=[
                    ToolCall("post_status", {"status": "blocked: quality-gate",
                                             "notes": f"mock scripted failure on {self.backend}"}, id="t1"),
                ], tokens_in=500, tokens_out=90, stop_reason="tool_use")
            return Turn(tool_calls=[
                ToolCall("save_output", {"relpath": "artifact.md",
                                         "content": f"# mock artifact\n({self.backend} @ {effort})"}, id="t1"),
                ToolCall("post_status", {"status": "completed",
                                         "notes": f"mock run on {self.backend} — artifact saved"}, id="t2"),
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


# --------------------------------------------------- openai-compatible
class OpenAICompatAdapter:
    """One adapter for every OpenAI-compatible chat endpoint. Covers:

    - GPT-5.6 Sol / Terra (backend 'gpt-5.6-sol' / 'gpt-5.6-terra')
    - GLM-5.2 via a provider base_url (backend 'glm-5.2'; z.ai and several
      other providers expose OpenAI-compatible endpoints)

    As with the Anthropic adapter, model IDs, base URLs, and any extra
    request params (e.g. how 'ultra' or effort maps onto the provider's
    reasoning controls) are runtime config — not hardcoded claims about a
    provider's current API. `effort_params` maps our effort string to a dict
    of extra request kwargs; unmapped efforts are folded into the system
    prompt so the adapter degrades instead of erroring.
    """

    def __init__(self, backend: str, model_id: str, api_key_env: str,
                 base_url: str | None = None,
                 effort_params: Dict[str, Dict] | None = None) -> None:
        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(f"{api_key_env} is not set — required for backend '{backend}'")
        import openai  # deferred
        self._client = openai.OpenAI(api_key=key, base_url=base_url) if base_url \
            else openai.OpenAI(api_key=key)
        self.backend = backend
        self._model = model_id
        self._effort_params = effort_params or {}

    def run_turn(self, system: str, messages: List[Dict], tools: List[Dict],
                 effort: str, max_tokens: int) -> Turn:
        extra = self._effort_params.get(effort)
        sys_text = system if extra is not None else f"{system}\n\n<effort>{effort}</effort>"
        oa_messages = [{"role": "system", "content": sys_text}] + _to_openai_messages(messages)
        kwargs: Dict = dict(model=self._model, messages=oa_messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = [{"type": "function",
                                "function": {"name": t["name"], "description": t["description"],
                                             "parameters": t["input_schema"]}} for t in tools]
        if extra:
            kwargs.update(extra)
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        turn = Turn(text=choice.message.content or "",
                    tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                    tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                    stop_reason=choice.finish_reason or "end_turn")
        for tc in choice.message.tool_calls or []:
            turn.tool_calls.append(ToolCall(tc.function.name,
                                            json.loads(tc.function.arguments or "{}"),
                                            id=tc.id))
        if choice.finish_reason == "content_filter":
            turn.safeguard_refusal = True
        return turn


def _to_openai_messages(messages: List[Dict]) -> List[Dict]:
    """Translate our Anthropic-shaped history into OpenAI chat format."""
    out: List[Dict] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in content if b.get("type") == "tool_use"]
            msg: Dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user message carrying tool results
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": b["content"]})
    return out


def serialize_tool_results(results: List[Dict]) -> Dict:
    """Build the user message carrying tool results back to the model."""
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r["id"], "content": json.dumps(r["result"])}
        for r in results
    ]}
