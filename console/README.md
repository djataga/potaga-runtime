# Potaga Console

The operator console for the Potaga runtime: overview, plan & tasks, routing,
conflicts, budget, agents, parameters, project switcher, onboarding, and the
ADR browser. Vite + vanilla JS, no framework.

Moved here from the prompt-pack repo (`djataga/potaga`), which is prompts and
governance only — "no runtime code by design." The console is runtime UI, so
it lives with the runtime. Its original standalone README is preserved as
`README.frontend.md`; generation manifests are under `manifests/`.

## Run it

```bash
cd console
npm install
npm run dev          # mock mode out of the box
```

## Data contract

`js/api.js` resolves data in two steps:

1. `GET {VITE_API_BASE_URL|/api}/dashboard/state` — the live runtime state
   (plan, tasks, decision log, backends, budget), and
   `PATCH /api/config/parameters` for operator edits.
2. On failure it falls back to `public/mock/dashboard-state.json`, so the
   console always renders.

## Wiring it to the runtime (next step, not yet implemented)

Everything the console needs already exists in `potaga_runtime`:

- `PlanStore` holds tasks, statuses, costs, and the Decision/Conflict logs —
  the `dashboard/state` payload is essentially `PlanStore` + `BudgetLedger.
  spent_by_backend` + `AvailabilityMonitor` statuses serialized to JSON.
- The `EventBus` is the live feed: subscribe and push events over SSE or a
  WebSocket for the decision-log stream and toasts.
- `Config.parameters` backs the parameters screen; a `PATCH` handler should
  write a versioned config diff, never a silent mutation.

A thin FastAPI/Starlette app (~100 lines) over those three objects turns the
mock console into the real one.
