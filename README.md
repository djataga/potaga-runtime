# potaga-runtime

![CI](https://github.com/djataga/potaga-runtime/actions/workflows/ci.yml/badge.svg)

Complete reference implementation (roadmap Phases 1 + 3–6) of the Potaga orchestration runtime. It consumes the
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

## Phase 4 additions — Memory & Persistence

| Module | Spec / policy | Notes |
|---|---|---|
| `memory.py` — `MemoryBackend` protocol | spec §9 | `FilesystemBackend` (default, GA) and `ClaudeMemoryStoresBackend`, a thin mapping onto the resources[]/hash-precondition shape of spec §9.3 with the concrete client injected by the operator — no hardcoded claims about any beta API's availability; verify at docs.claude.com before wiring it. |
| Optimistic concurrency | §B.3, spec §9.4 | `occ_update()` — read-modify-write with SHA-256 preconditions, ≤5 retries, then `OCCExhausted` for Orchestrator escalation. Stale-hash writes raise `PreconditionFailed`; interleaved writers lose nothing. |
| Immutable versions + provenance | spec §9.5 | every write lands a version copy + metadata (writer, model, session, sha256, ts) under `<store>/.versions/` — the full audit trail, queryable via `history()`. |
| Session attachment | spec §9.3 | `attachments(agent)` renders the agent's mounts (store, access, instructions) in the resources[] shape; the session opening now states exactly what is mounted with which access. |
| Context eviction + recovery | spec §9.6, preamble | the runner saves working state to the agent's cache partition FIRST, then compacts history (opening + last two exchanges kept, intermediate tool traffic evicted). A later session for the same subtask gets the scratch injected under a RECOVERY PROTOCOL header. |
| Cross-session resume | roadmap Phase 4 | `Orchestrator.resume()` / `potaga resume` parses the persisted plan back (statuses, models, spend), resets dead `in_progress` tasks, optionally retries non-safeguard blocks (`--retry-blocked`) — safeguard blocks are never auto-retried across sessions (§B.6). |

## Phase 5 additions — Conflict Resolution

| Module | Protocol / policy | Notes |
|---|---|---|
| `conflicts.py` — cards + scoring | Escalation Protocols §2 | conflict cards with per-agent independent scores averaged into the shared formula `org − risk − reversibility + evidence`; hard constraints tracked per option; rendered per the CONFLICT_CARD template into the plan's Conflict Log. |
| L0 local resolution | §3 | accept only when the top option beats the runner-up by ≥ `local_acceptance_margin` (15%) with no hard-constraint violation; security-relevant cards auto-escalate past L0 regardless of margin. |
| L1 tie-breaks | §5, policy §B.7 | the exact order: Security Override (more secure wins regardless of score) > Cost Ceiling Override (≥80% pressure → cheapest) > Deadline Override (fastest on a blocked critical path) > Reversibility/Evidence Preference (reversible → keep options open; irreversible → strongest evidence) > Reviewer Authority (binding arbiter for Coder↔Tester quality disputes, rotation-aware). |
| L2 / L3 hooks | §4 | judgment enters only via injected callables — an Architect hook (may be LLM-backed) and the human hook, which emits `[HUMAN REQUIRED]` and pauses. Without a human hook the ladder terminates on the safest option, so termination in ≤4 hops is guaranteed. |
| Dependency cycles | §7.1, policy §B.8 | topological detection after decomposition; each cycle broken at its lowest-priority edge (never a security task's dependency), marked optional, logged as an auto-resolved conflict card. |
| Waiting-cycle scan | §7.2–7.4 | `scan_waiting_cycles()` — the 60-second scan's core: detects agent wait cycles and names the lowest-priority victim for preemption (state save to its cache partition is the caller's job). Ready for parallel dispatch. |

## Phase 6 additions — Safety Guardrails & Gates

| Module | Policy / spec | Notes |
|---|---|---|
| `gates.py` — GateEngine | §B.4, spec §11 | post-merge: Coder completion requires `sandbox_verified` and scope ≤1.3× (`[SCOPE REJECTION: ratio]` on breach); Tester completion requires coverage ≥ threshold; Reviewer verdicts parsed — `[REJECTED: …]` re-opens the coder dependency with feedback (max 2×, then `blocked: needs-human` + `[HUMAN REQUIRED]`), and a review with **no explicit verdict is treated as rejection** (never approve silently). Pre-dispatch: docs tasks wait for reviewer `[APPROVED]`. |
| Gate blocks ≠ model failures | §B.5 boundary | coverage/sandbox/scope/review blocks are quality outcomes: terminal for the dispatch, never walked down the fallback chain. |
| `tools/sandbox.py` | spec §11.5 | `run_code`: Python in an isolated subprocess (`python -I`), jailed to the task workdir under the code store, wall-clock timeout, CPU/memory rlimits, stripped env, socket creation disabled in the child. Honest scope: process-level isolation for a reference runtime — production needs container/VM isolation; this module is that seam. |
| Tool access matrix | spec §8.4 | `run_code` granted to coder/tester/reviewer/docs only; architect/research never see it, and out-of-allowlist calls are still blocked pre-execution. |
| Token budgets | spec §11.2 | per-subtask output budget (est_tokens_out × 3, floor 2048); overflow interrupts the turn loop, posts `blocked: token-budget`, and logs a budget event — retry/re-route stays with the Orchestrator. |

With Phase 6, all ten policy points of `prompts/07_orchestrator.md` §B have running code
behind them. Remaining beyond the roadmap: true parallel dispatch (the waiting-cycle
scanner from Phase 5 is ready for it) and container-grade sandbox isolation.

## Console

`console/` holds the operator UI (moved from the prompt-pack repo, which is
prompts-and-governance only). `cd console && npm install && npm run dev` runs it
in mock mode; see `console/README.md` for the runtime API contract it expects.

## CI as a cross-repo integration test

`.github/workflows/ci.yml` pins a prompt-pack ref (default `v4.2.0`), first runs a
**pack-compat** job (can the runtime's config validator boot against that pack?),
then the full test suite with `POTAGA_REPO` pointing at the clone. If a pack change
violates a runtime invariant, this build goes red with a named reason — bumping the
pin is a deliberate act, never a surprise. The `v4.2.0` tag is live, so this
goes green on the first push.

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

59 tests, offline (sandbox tests run real subprocesses): config invariants (including rejection of the dead-xhigh config the
v4.2 audit fixed), pricing-epoch switching, multiplier math, degraded-mode routing, the
security floor, plan parsing and single-writer rendering, store grants and path-escape
protection, and an end-to-end dry run asserting artifacts, provenance, statuses, and the
Decision Log. Phase 3 adds: frontend_ui classification routing to GLM, the quality
threshold keeping backend coding on Sonnet despite cheaper GLM, the security path
never flipping on cost, the ultra cap falling back to the Opus floor, notify-once
degraded transitions, chain-walk escalation completing on the fallback tier, and
safeguard refusals never being re-dispatched. Phase 4 adds: precondition rejection,
OCC retry-through-interference and exhaustion, version-history provenance, per-agent
attachment rendering, eviction saving scratch before compaction, recovery injection,
an interrupted-project resume completing end-to-end, safeguard blocks surviving
--retry-blocked, and a rendered-plan parse round-trip. Phase 5 adds: L0 margin
acceptance and sub-margin escalation, the spec's Conflict #001 scenario (huge margin
overridden by a security hard constraint), every L1 tie-break in order, binding
arbiter decisions, L2/L3 hooks with the [HUMAN REQUIRED] event, cycle breaking that
spares security tasks, wait-cycle victim selection, and resolved cards rendered into
the plan. Phase 6 adds: sandbox execution/network-block/timeout, the run_code round
trip through the turn loop, per-agent tool grants, token-budget interruption,
coverage/sandbox/scope gates, the reviewer rejection→re-open→needs-human loop,
silent reviews treated as rejections, the docs approval gate, and an e2e proof that
gate blocks never trigger model-chain escalation.

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
