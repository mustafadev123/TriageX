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

| Area | State |
|---|---|
| Deterministic surge simulation (I-85 pileup, seed 42) | Complete, tested |
| FastAPI API + WebSocket, command-center dashboard | Complete, tested |
| Tiger Data persistence (TimescaleDB hypertable) | Complete; migrations 001–005 applied to the live service |
| FaceTime agent (Chert + LiveKit + Gemini Live), real caller video | Verified on real calls |
| Staff-card reading from live FaceTime video | Verified on a real call (5 cards, blank page) |
| Card → roster check → spoken "I confirm" → dashboard | Built and tested end to end over HTTP; full live call pending |
| iMessage on the Chert hackathon line | Built (mocked tests); key accepted by Chert; live round trip pending |
| Grounded AI operational brief (Gemini, template fallback) | Verified against the real Gemini API |

Evidence and open items: [docs/chert-status.md](docs/chert-status.md). The demo:
[docs/demo-script.md](docs/demo-script.md). API: [docs/api.md](docs/api.md). Plan and
assumptions: [docs/plan.md](docs/plan.md), [docs/assumptions.md](docs/assumptions.md).

## Try the simulation

```bash
cd backend
uv run python -m app.simulation.cli i85_pileup --seed 42
uv run python -m app.simulation.cli heat_wave --seed 7 --staff 0 --no-level-loading
```

## Layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app, simulation engine, integrations, tests (Python 3.12, uv) |
| `frontend/` | React + TypeScript + Vite + Tailwind dashboard |
| `agent/` | Chert FaceTime agent (Node, LiveKit Agents, Gemini Live + Gemini vision) |
| `db/migrations/` | Plain SQL migrations for Tiger Data |
| `docs/` | Handoff, plan, assumptions, demo script, status, printable test cards |
| `scripts/` | Dev and verification scripts |

## Prerequisites (macOS)

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for the backend)
- **Node.js 24** (`/opt/homebrew/opt/node@24/bin` first on `PATH`; Homebrew's Node 25 is broken)
- An iPhone verified for the Chert project, and the Chert CLI via `npx @trychert/facetime@0.1.0-beta.21`

## Setup

```bash
cp .env.example .env                                 # backend keys (see below)
cp agent/.env.example agent/.env.local && chmod 600 agent/.env.local
(cd backend && uv sync)
(cd frontend && npm install)
(cd agent && npm install)
```

Keys (never commit them or paste them anywhere): the repo `.env` holds `DATABASE_URL` (optional),
`TRIAGEX_AGENT_TOKEN` (same value as in `agent/.env.local`; `openssl rand -hex 32`),
`GEMINI_API_KEY` (AI brief), and for iMessage `CHERT_API_KEY`, `CHERT_PROJECT_ID`,
`CHERT_LINE_ID` (`cd backend && uv run python ../scripts/chert_inspect.py`) and
`DEMO_ALLOWED_PHONES`. `agent/.env.local` holds the LiveKit keys, `GOOGLE_API_KEY`,
`TRIAGEX_AGENT_TOKEN` and `AGENT_NAME`. Every integration works without its keys and says so.

## Run (four terminals)

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev                      # http://localhost:5173
cd agent && npm start
cd agent && npx --yes @trychert/facetime@0.1.0-beta.21 dev --agent triagex-facetime-agent --minutes 30
```

Stop `chert dev` only with Ctrl+C and wait for "Stopped". The dashboard proxies `/api` and `/ws`
to `127.0.0.1:8000`; the API docs are at http://127.0.0.1:8000/docs. On Windows,
`scripts/dev.ps1` starts the backend and the dashboard.

## Test

```bash
cd backend && uv run pytest -q && uv run ruff check .     # DB tests start a local PostgreSQL
cd frontend && npm test && npm run lint && npm run build
cd agent && npm test
```

Rehearsal (needs the backend and `npm run dev` running; Chrome on macOS, Edge elsewhere):

```bash
cd frontend
npm run rehearse -- /tmp/rehearsal 1366 768                          # simulated replies
REHEARSE_CONFIRM=agent npm run rehearse -- /tmp/rehearsal 1366 768   # agent API, no phone
REHEARSE_CONFIRM=live  npm run rehearse -- /tmp/rehearsal 1366 768   # waits for a real call / iMessage
```
