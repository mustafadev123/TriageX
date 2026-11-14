# TriageX API (Phase 3)

This is the backend contract for the single-screen command center. Every number in a
response is computed by the deterministic simulation; the frontend never calculates
deltas or percentages itself.

- Base URL in development: `http://127.0.0.1:8000`. Use `127.0.0.1`, not `localhost`. On
  Windows, `localhost` tries IPv6 first and adds about 200 ms to every request.
- The Vite dev server proxies `/api` and `/ws` to the backend.
- Interactive schema: `http://127.0.0.1:8000/docs` (OpenAPI).

## Demo flow in four calls

| Step | Call | What changes |
|---|---|---|
| 1. Reset | `POST /api/demo/reset` | Fresh I-85 run, seed 42. Both futures are identical. |
| 2. Approve outreach | `POST /api/runs/{run_id}/proposals/{proposal_id}/approve` with `{"at_tick": <playhead tick>}` | Creates a staffing request. No outcome changes yet. |
| 3. Staff confirms (repeat) | `POST /api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply` | Adds one confirmation and recalculates Track B. |
| 4. Read diagnostics (optional) | `GET /api/runs/{run_id}/diagnostics` | Engineering detail for judge Q&A. |

Every `POST` returns the complete `DashboardState`, so the screen re-renders from the
response without another request.

For step 2, use the card with `recommended: true` in `proposals`. Send `at_tick`, the
simulated tick at which the operator clicked Approve (the playhead position).

- `at_tick` is required. The server never assumes the approval happened at the proposal
  tick.
- It must be no earlier than the proposal tick.
- The 45-minute staffing lead time starts at `at_tick`.

In step 3, each confirmation counts in the simulation from the approval tick plus 45
simulated minutes. That holds no matter when the reply arrives in real time. History is
never rewritten: ticks before that point are identical in both tracks.

I-85 example timeline:

| Event | Simulated time |
|---|---|
| Surge Predicted | 16:15 |
| Outreach Approved (`at_tick` 18) | 16:30 |
| Staff en route (lead-time band) | 16:30–17:15 |
| Shift Active, Futures Diverge | 17:15 |

A request reports `proposal_clock`, `approved_clock` and `effective_clock`. Feed events
for confirmations sit at the approval tick, inside the lead-time band.

Measured on the development laptop (live uvicorn, curl, 5 rounds × 3 replies):

| Operation | Time |
|---|---|
| One confirmation, full HTTP round trip | 25.7–28.2 ms (about 12 ms recalculating Track B, 4–6 ms building the payload, the rest serialization and transport) |
| Reset | about 47 ms |

The payload is about 245 KB uncompressed and about 12 KB with gzip.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/health` | Liveness, branding, synthetic-data notice |
| `GET /api/integrations/health` | Simulation `ok`. Database, AI brief (`ai_primary`, `ai_fallback`), Chert iMessage (`messaging`) and the FaceTime agent (`facetime`) report `not_configured`, `starting`, `ok` or `error`; `ok` only after a real successful check or use. |
| `GET /api/hospitals` | Map pins (synthetic capacity) |
| `GET /api/scenarios` | Scenario selector |
| `POST /api/demo/reset` | Body (optional): `{scenario_id, seed}`. Discards all runs and returns a new, identical initial state |
| `GET /api/demo/state` | The active run's state (for a second screen or a page refresh) |
| `POST /api/runs` | Body: `{scenario_id, seed}`. New run without discarding others |
| `GET /api/runs/{run_id}` | Current `DashboardState` |
| `POST /api/runs/{run_id}/proposals/{proposal_id}/approve` | Body: `{at_tick, operator?}`. `at_tick` (required) is the simulated approval time |
| `POST /api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply` | Body (optional): `{staff_id, reply: "IN"}`. Recorded as `simulated` |
| `GET /api/runs/{run_id}/diagnostics` | Assumptions, limitations, forecast accuracy, all proposals, digests, recompute timings |
| `GET /api/db/status`, `GET /api/runs/{run_id}/db/insights` | Tiger Data status and stored-row queries (see [database.md](database.md)) |
| `GET /api/meta/provenance` | Running git commit and the Entire checkpoint refs in the repository |
| `POST /api/runs/{run_id}/brief` | Body: `{at_tick}`. Grounded operational brief from the state visible at `at_tick` (Gemini primary → Gemini fallback → template). Numbers are filled in by the server |
| `GET /api/runs/{run_id}/brief` | The latest brief for the run, or 404 |
| `GET /api/agent/active-request` | FaceTime agent only (header `X-TriageX-Agent-Token`). The open staffing request, or 404 |
| `POST /api/agent/call` | Agent: `{call_id, event: connected \| ended}` |
| `POST /api/agent/card-check` | Agent: `{call_id, card_id, name, role, availability, confidence}` → `{valid, reason, message, staff_id, display_name}`. Roster: `backend/app/data/staff_roster.json` |
| `POST /api/agent/confirm` | Agent: `{call_id, card_id, verbal_confirmation: "I confirm"}`. Needs a valid card check on the same call within 2 minutes; records one `facetime` confirmation through the same path as the simulated reply; idempotent (409 with a reason otherwise) |

Error codes:

- **404:** unknown run, proposal, request or staff member.
- **409:** a state conflict. That covers approving a proposal twice, approving before
  the proposal tick, a reply other than `IN`, a duplicate reply, a fully confirmed
  request, or an expired request (10 minutes of real time).
- **422:** invalid input, including a missing `at_tick`.

## WebSocket `/ws`

Every message is JSON, sent from the server to the client:

```json
{"type": "hello", "run_id": "…", "version": 3}
{"type": "run.updated", "run_id": "…", "version": 4, "reason": "staff_confirmed", "recompute_ms": 12.4}
```

- `reason` is one of `reset`, `run_created`, `proposal_approved` or `staff_confirmed`.
- A screen that did not make the change refetches `GET /api/runs/{run_id}` when the
  version increases.
- The client may send `{"type": "ping"}` and receives `{"type": "pong"}`.

## `DashboardState` at a glance

| Field | Renders | Notes |
|---|---|---|
| `run` | Header | Includes `notice`, the synthetic-data label that must stay on screen |
| `timeline` | Slider | `clocks[tick]` labels, `demo_window` (24 ticks, 15:00–21:00 for I-85), `suggested_ms_per_tick` |
| `hospitals` + `series` | Map | Per hospital, 96 points per track: status, saturation, queue, ambulances waiting, staff, forecast, diverting |
| `curves` | Comparison charts | Demo window only, both futures side by side; see below |
| `bands` | Shaded chart regions | `demo_window`, `staff_lead_time` (approval to shift start), `shift_active`, `level_loading_active` |
| `markers` | Vertical chart lines | "Surge Begins", "Surge Predicted", "Outreach Approved", "Shift Active", "Futures Diverge"… |
| `transitions` | Map color changes | Status changes per track and hospital |
| `flows` | Map arcs | Rerouted ambulances per tick: `diversion` or `level_loading` |
| `feed` | Coordination feed | Concise titles, including Outreach Proposed and Staff En Route; `track`, `origin` and `severity` included |
| `proposals` | Action cards | Demo-window cards plus the recommended one |
| `staffing_requests` | Outreach panel | Requested, confirmed and usable staff, status, confirmations with `source` |
| `impact` | Hero metric | `primary` = ambulance hours lost to handoff delays; `secondary` = average wait and diverted ambulances; `recovery` |
| `divergence` | "Futures Diverge" moment | `null` until an intervention changes the future |
| `meta` | Dev overlay | `recompute_ms`, `build_ms` |

### `curves.points[]` (one per demo-window tick)

| Field | Meaning |
|---|---|
| `phase` | `before_intervention`, `staff_en_route`, `shift_active` or `after_shift`. Use it to style the curve. |
| `diverged` | `false` for every tick before the first difference. Both lines draw on top of each other until then. |
| `baseline_*` / `triagex_*` | Focus-hospital saturation, network patients waiting, network ambulances waiting, cumulative ambulance-hours |
| `ambulance_hours_saved_so_far` | The running hero counter. It is 0 until the futures diverge and equals `impact.primary` at the window's end. |

**Honest playback rule:** while the playhead is at tick `t`, show
`curves.points[t].ambulance_hours_saved_so_far` as the live counter. Show the final
`impact` figures only when playback reaches the end of the window. The counter can then
never show an improvement before the intervention is active.

### Hero metric definitions

| Metric | Definition |
|---|---|
| **Ambulance hours lost to handoff delays** (primary) | Sum over demo-window ticks of ambulances waiting for a treatment space at the end of each tick × 15 min. Network-wide, derived from tick state. |
| **Average patient wait** | Mean wait from ED arrival to treatment space for patients arriving in the window. Patients still waiting at the end of the horizon count with their wait so far. |
| **Ambulances diverted** | Ambulances rerouted by diversion, for arrivals in the window |
| **Recovery** | First tick after the focus hospital's first breach that starts a full simulated hour below 85% saturation |

Each `HeroMetric` carries `baseline`, `triagex`, `delta` (TriageX minus baseline),
`pct_change`, `improved` (`null` when equal) and a ready-made `headline`, for example
"9.25 fewer ambulance hours lost (−36%)". When the two values are equal, the headline
reads "No difference yet" before any intervention and "No change" after one.

Simulated replies always have `source: "simulated"` in the request and
`origin: "simulated_reply"` in the feed, and their detail ends with "via simulated
reply". Nothing is labeled as a sent or received message until the Chert integration
exists (Phase 6).

## Current I-85 result (seed 42)

The recommended Grady staffing proposal is raised at 16:15 and approved at 16:30. Track B
changes only after the confirmed staff start their shift at 17:15.

| Confirmed staff | Primary headline | Average wait | Ambulances diverted | Recovery |
|---|---|---|---|---|
| 1 | 5.50 fewer ambulance hours lost (−22%) | 3.8 min shorter (−27%) | No change | 30 min sooner |
| 2 | 8.25 fewer ambulance hours lost (−32%) | 5.8 min shorter (−41%) | 3 fewer (−30%) | 45 min sooner |
| 3 | 9.25 fewer ambulance hours lost (−36%) | 6.8 min shorter (−48%) | 3 fewer (−30%) | 45 min sooner |

These are model outputs, not targets. Other scenarios and seeds give different results;
see [assumptions.md](assumptions.md).
