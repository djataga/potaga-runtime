# Potaga Frontend

## Commands

- `npm install`
- `npm run dev`
- `npm run build`
- `npm run preview`
- `npm run lint`

## Data flow

The frontend tries `VITE_API_BASE_URL + /dashboard/state` first and falls back to `public/mock/dashboard-state.json` for local development.

## Config saves

Parameter edits attempt `PATCH /api/config/parameters`. If the backend is unavailable, the UI keeps the diff as a local preview and shows a status toast.

## Screens

Includes Projects, Overview, Onboarding, Plan, Routing, Conflicts, Budget, Agents, ADR Browser, and Parameters.
