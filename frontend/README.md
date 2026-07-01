# AgentAlloy Web UI

React SPA (Vite 6 + React 18 + TypeScript + Tailwind v3) for AgentAlloy config, telemetry, diagnostics, and health.

- Dev: `pnpm install && pnpm dev` (serves on :5173; run the FastAPI service on :47950 in parallel).
- Build: `pnpm build` (`tsc && vite build` → `dist/`, served as static files by the FastAPI process).
- Proxy: the Vite dev server forwards `/api`, `/telemetry`, `/health`, `/readiness`, and `/diagnostics` to `http://localhost:47950`.

Routes (HashRouter, no auth): `#/config`, `#/telemetry`, `#/diagnostics`, `#/health`. Data is polled via React Query (30s refetch).
