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
| 2. Approve outreach | `POST /api/runs/{run_id}/proposals/{proposal_id}/approve` | Creates a staffing request. No outcome changes yet. |
| 3. Staff confirms (repeat) | `POST /api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply` | Adds one confirmation and recalculates Track B. |
| 4. Read diagnostics (optional) | `GET /api/runs/{run_id}/diagnostics` | Engineering detail for judge Q&A. |

Every `POST` returns the complete `DashboardState`, so the screen re-renders from the
response without another request.

For step 2, use the card with `recommended: true` in `proposals`.

In step 3, each confirmation counts in the simulation from the approved request tick
plus 45 simulated minutes. That holds no matter when the reply arrives in real time.
History is never rewritten: ticks before that point are identical in both tracks.

Measured on the development laptop (live uvicorn, curl, 5 rounds × 3 replies):

| Operation | Time |
|---|---|
| One confirmation, end to end | 25–30 ms (about 12 ms recalculating Track B, about 4 ms building the payload) |
| Reset | about 47 ms |

The payload is about 245 KB uncompressed and about 12 KB with gzip.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/health` | Liveness, branding, synthetic-data notice |
| `GET /api/integrations/health` | Simulation `ok`; database, AI and Chert report `not_implemented` until their phases |
| `GET /api/hospitals` | Map pins (synthetic capacity) |
| `GET /api/scenarios` | Scenario selector |
| `POST /api/demo/reset` | Body (optional): `{scenario_id, seed}`. Discards all runs and returns a new, identical initial state |
| `GET /api/demo/state` | The active run's state (for a second screen or a page refresh) |
| `POST /api/runs` | Body: `{scenario_id, seed}`. New run without discarding others |
| `GET /api/runs/{run_id}` | Current `DashboardState` |
| `POST /api/runs/{run_id}/proposals/{proposal_id}/approve` | Body (optional): `{at_tick, operator}`. `at_tick` defaults to the proposal tick |
| `POST /api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply` | Body (optional): `{staff_id, reply: "IN"}`. Recorded as `simulated` |
| `GET /api/runs/{run_id}/diagnostics` | Assumptions, limitations, forecast accuracy, all proposals, digests, recompute timings |

Error codes:

- **404:** unknown run, proposal, request or staff member.
- **409:** a state conflict. That covers approving a proposal twice, approving before
  the proposal tick, a reply other than `IN`, a duplicate reply, a fully confirmed
  request, or an expired request (10 minutes of real time).
- **422:** invalid input.

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
| `feed` | Coordination feed | Concise titles; `track`, `origin` and `severity` included |
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
"11.5 fewer ambulance hours lost (−45%)".

## Current I-85 result (seed 42)

The run approves the recommended Grady staffing proposal at 16:15. Track B changes only
after the confirmed staff start their shift at 17:00.

| Confirmed staff | Primary headline | Average wait | Recovery |
|---|---|---|---|
| 1 | 6.5 fewer ambulance hours lost (−26%) | 4.2 min shorter (−29%) | 15 min sooner |
| 2 | 9.8 fewer ambulance hours lost (−38%) | 6.2 min shorter (−44%) | 45 min sooner |
| 3 | 11.5 fewer ambulance hours lost (−45%) | 7.4 min shorter (−52%) | 45 min sooner |

These are model outputs, not targets. Other scenarios and seeds give different results;
see [assumptions.md](assumptions.md).
