# potaga-runtime

Phase-1 + Phase-3 reference implementation of the Potaga orchestration runtime. It consumes the
[potaga prompt pack](https://github.com/djataga/potaga) (v4.2) and wires the dispatch path end-to-end.
Phase 1: agent loop, planning document, task decomposition. Phase 3: multi-model
routing — the five-stage CQP pipeline, OpenAI-compatible adapters (GPT-5.6 Sol/Terra,
GLM-5.2), Sol Ultra containment, and full §B.5 fallback-chain escalation.

## What is implemented (and which policy point it satisfies)

| Module | Policy §B | Notes |
|---|---|---|
| `orchestrator.py` — loop + Decomposer | 5 | The **only** LLM call the control plane makes is decomposition, using the §A prompt extracted verbatim from `prompts/07_orchestrator.md`. One same-tier retry on failure. |
| `router.py` — Router + AvailabilityMonitor | 1, 9 | Loads the full routing matrix; walks each chain skipping unavailable backends; enforces the security floor (a security task with no floor-qualifying backend **fails loudly** rather than routing below Opus 4.8); emits `degraded-mode` when a fallback becomes primary. |
| `router.py` — BudgetLedger | 2, 10 | Reserve-at-dispatch with effective = logical × loop(10) × Ultra(3.5) × tokenizer; 80% soft warning, 90% hard pause for user confirmation; pricing-epoch switch on 2026-09-01 via `parameters.yaml`. |
| `plan.py` — PlanStore | 3 | Single writer of `MULTI_AGENT_PLAN.md`. Agents post status to their `potaga-cache` partition; the Orchestrator merges. Subscribes to the event bus and persists every event into the Decision Log. |
| `sessions/runner.py` | 6 | Turn loop with per-backend timeouts, tool allowlist enforcement (out-of-grant tools blocked before execution), and safeguard handling: refusals recorded verbatim, task marked `blocked: safeguard`, content never re-dispatched. |
| `sessions/runner.py` — SessionBuilder | — | `00_shared_preamble.md` + role prompt + injected subtask contract, exactly as the repo README specifies. |
| `memory.py` | — | Seven filesystem-backed stores with per-agent write grants fixed at session creation, path-escape protection, and provenance sidecars on every write. Swappable for Claude Memory Stores in Phase 4. |
| `orchestrator.py` — architecture gate | 4 (partial) | The mandatory human approval gate after the Architect completes; declining blocks all downstream tasks. Full gate engine arrives in Phase 6. |
| `config.py` | — | Boot-time validation mirrors the repo's CI invariants — the runtime refuses to start on a config the CI would reject. |

## Phase 3 additions

| Module | Policy / spec | Notes |
|---|---|---|
| `router.py` — five-stage pipeline | §B.1, spec §4.4 | classify (agent + UI-content hints) → availability filter → quality threshold (80% of best available, scores from spec §4.1, operator-tunable) → CQP scoring with the `cqp_margin` tie-break → fallback assignment in declared order. |
| Special rules | spec §4.5 | Security-Critical Path picks the highest-quality qualifying backend regardless of CQP; cost-ceiling preference flips non-critical close races to the cheaper option at ≥80% budget pressure; the security floor still fails loudly when nothing qualifies. |
| `SolUltraGovernor` | spec §5 | sol-ultra calls serialized (lock) and capped per project (default 6, from `special_rules`); when the cap is exhausted, ultra entries are skipped as if unavailable. The ×3.5 multiplier is priced into every estimate. |
| `AvailabilityMonitor` | §B.9 | dynamic statuses with `set_status()` as the poller hook; each down-transition swaps routing to GA fallbacks and is announced once per session. |
| Orchestrator dispatch | §B.5 | try the CQP-chosen primary → one same-tier retry → walk the fallback chain tier by tier with FALLBACK/ESCALATION events → `blocked: chain-exhausted`. Safeguard refusals remain terminal — never walked down the chain. |
| `OpenAICompatAdapter` | — | one adapter for every OpenAI-compatible endpoint: Sol/Terra directly, GLM-5.2 via `--glm-base-url`. Model IDs, base URLs, and effort→param mappings are runtime config, never hardcoded provider claims. Lights up automatically when `OPENAI_API_KEY` / `ZAI_API_KEY` are present. |
| Cost tracking | §B.2 | per-backend spend breakdown in the ledger and the CLI summary. |

**Still ahead** (seams in place): Claude Memory Stores backend (Phase 4), the conflict
ladder and deadlock scan (Phase 5), the full gate engine and code sandbox (Phase 6),
true parallel dispatch.

## Quick start

```bash
pip install -e ".[dev]"
git clone https://github.com/djataga/potaga  # the prompt pack

# Offline — full dispatch path against the mock adapter
potaga run "Build a REST API for tasks with JWT auth" \
    --prompts ./potaga --workspace ./ws --dry-run --yes

# Live — against the Anthropic API
export ANTHROPIC_API_KEY=...
potaga run "Build a REST API for tasks with JWT auth" \
    --prompts ./potaga --workspace ./ws \
    --model-id <a model string your account can access> [--effort-param]
```

**On `--model-id` / `--effort-param`:** the spec targets `claude-sonnet-5` with an `effort`
parameter. This runtime does not hardcode either as fact about your API deployment — pass
the model string your account can access, and enable `--effort-param` only if your
deployment accepts it (otherwise effort is folded into the system prompt and the adapter
degrades gracefully). Check current model names at https://docs.claude.com.

## Tests

```bash
POTAGA_REPO=/path/to/potaga python -m pytest tests/ -q
```

23 tests, fully offline: config invariants (including rejection of the dead-xhigh config the
v4.2 audit fixed), pricing-epoch switching, multiplier math, degraded-mode routing, the
security floor, plan parsing and single-writer rendering, store grants and path-escape
protection, and an end-to-end dry run asserting artifacts, provenance, statuses, and the
Decision Log. Phase 3 adds: frontend_ui classification routing to GLM, the quality
threshold keeping backend coding on Sonnet despite cheaper GLM, the security path
never flipping on cost, the ultra cap falling back to the Opus floor, notify-once
degraded transitions, chain-walk escalation completing on the fallback tier, and
safeguard refusals never being re-dispatched.

## Layout

```
potaga_runtime/
├── events.py            # typed event bus — every module emits, PlanStore persists
├── config.py            # yaml loading + CI-mirror validation + pricing epoch
├── plan.py              # Task model, plan schema parser, single-writer PlanStore
├── router.py            # Router, AvailabilityMonitor, BudgetLedger
├── memory.py            # 7 filesystem stores, grants, provenance
├── orchestrator.py      # Decomposer (the one LLM call) + the deterministic loop
├── sessions/
│   ├── runner.py        # SessionBuilder + AgentRunner (turn loop, tools, safeguards)
│   └── adapters/core.py # Adapter protocol, MockAdapter, AnthropicAdapter
└── cli.py               # intake, event tail, human checkpoints, cost summary
```

MIT, same as the prompt pack.
