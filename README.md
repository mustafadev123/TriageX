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

Phase 1 (repository setup) is complete: both apps run and their test suites are wired. The
simulation, database, AI and messaging integrations are not implemented yet. See
[docs/plan.md](docs/plan.md) for the full plan and phase order.

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

The dashboard is at http://localhost:5173 and proxies `/api` to the backend on port 8000.

## Test

```powershell
cd backend;  uv run pytest;  uv run ruff check .
cd frontend; npm test;       npm run build
```
