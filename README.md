# TriageX

**Every ER capacity tool shows you the problem. TriageX calls the people who can fix it.**

TriageX is an emergency department surge forecasting and active coordination demo built for the
Healthcare Track at Emory Hacks 2026. It pairs a deterministic surge simulation of a five-hospital
Metro Atlanta network with human-approved staffing outreach and patient level-loading, then shows
the modeled before-versus-after outcome.

> **Synthetic hospital operations simulation — not real-time hospital data.**
> Hospital names and locations are used only for geographic context. Every operational figure is
> synthetic. No hospital has supplied data to, approved, or partnered with this project, and TriageX
> is not a clinically validated operations platform.

## Status

- Phase 1 (repository setup) is complete.
- Phase 2 (deterministic simulation engine) is complete.
- Phase 3 (FastAPI dashboard API and WebSocket) is complete.
- Phase 4 (Tiger Data persistence) is implemented and verified on local PostgreSQL. Live
  Tiger Data verification needs `DATABASE_URL`; see [docs/database.md](docs/database.md).
- The frontend command center is complete. See [docs/demo-script.md](docs/demo-script.md)
  for the 90-second walkthrough and [docs/screenshots/](docs/screenshots/).
- The AI and messaging integrations are not implemented yet.

See [docs/plan.md](docs/plan.md) for the plan,
[docs/assumptions.md](docs/assumptions.md) for every simulation assumption, and
[docs/api.md](docs/api.md) for the dashboard API.

## Try the simulation

```powershell
cd backend
uv run python -m app.simulation.cli i85_pileup --seed 42
uv run python -m app.simulation.cli heat_wave --seed 7 --staff 0 --no-level-loading
```

## Layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app, simulation engine, integrations, tests (Python 3.12, uv) |
| `frontend/` | React + TypeScript + Vite + Tailwind dashboard |
| `db/migrations/` | Plain SQL migrations for Tiger Data |
| `docs/` | Plan, assumptions, demo script |
| `scripts/` | Dev and verification scripts |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for the backend automatically)
- Node.js 20.19 or later, with npm

## Setup

```powershell
copy .env.example .env      # then fill in keys as each phase needs them

cd backend
uv sync

cd ..\frontend
npm install
```

## Run

```powershell
.\scripts\dev.ps1
```

Or start each side yourself:

```powershell
cd backend;  uv run uvicorn app.main:app --reload --port 8000
cd frontend; npm run dev
```

The dashboard is at http://localhost:5173 and proxies `/api` and `/ws` to the backend at
`127.0.0.1:8000`. The API's interactive docs are at http://127.0.0.1:8000/docs.

## Test

```powershell
cd backend;  uv run pytest;  uv run ruff check .   # DB tests start a local PostgreSQL
cd frontend; npm test;       npm run build;  npm run lint
cd frontend; npm run rehearse -- rehearsal 1366 768   # Edge walkthrough; needs both servers running
```

The rehearsal uses Microsoft Edge by default. On macOS (or without Edge), use Google Chrome:

```bash
cd frontend && REHEARSE_CHANNEL=chrome npm run rehearse -- /tmp/rehearsal 1366 768
```
