# TriageX — Phase 0 Plan

**Every ER capacity tool shows you the problem. TriageX calls the people who can fix it.**

TriageX is an emergency department surge forecasting and active coordination
demo for the Healthcare Track at Emory Hacks 2026. The architecture, stack, simulation design and
phases below are unchanged by the rename.

Product loop: **Observe → Forecast → Coordinate → Confirm → Simulate Intervention → Measure Outcome**

Every operational figure is synthetic. The dashboard always shows:
*"Synthetic hospital operations simulation — not real-time hospital data."*

## Design decisions that differ from the original brief

1. **Live reply vs. determinism.** The simulation is a pure function
   `simulate(scenario, seed, interventions[])`. Track B is re-run in full on each confirmation. A
   confirmation takes effect at request sim-time plus a fixed lead, never at wall-clock time.
2. **Track B before anyone acts equals baseline exactly.** The lines diverge only once
   interventions are approved and confirmed. A "scripted response" replay, labeled synthetic,
   covers the Time Machine.
3. **Baseline gets reactive diversion**, as real EDs do. Otherwise TriageX wins by construction.
4. **The forecast is a simple projection** that may not read the scenario's future demand curve.
   The UI labels scenario-known, forecast and observed demand separately.
5. **Level-loading redirects inbound ambulance patients before arrival**, with a travel-time
   penalty. Admitted patients are not moved.
6. **Chert is treated as iMessage-only.** SMS fallback is not in the official docs.
7. **The Chert webhook needs a public HTTPS URL**, so local dev needs a tunnel.
8. **Metrics are stored at 15-minute ticks** and rolled up hourly in SQL with `time_bucket`.
9. **Patient navigation is cut from the MVP.** At most the deterministic safety screen ships.
10. **Deployment is a stretch goal.** The demo runs from a laptop with a tunnel.

## A. Architecture

One FastAPI process, one React single-page app, one Tiger Postgres service, three outbound
integrations.

```
React SPA ──REST──> FastAPI ──> simulation (pure, in-process)
    ^  └──WS──────<   │  ├────> Tiger Data (psycopg, plain SQL)
    │                 │  ├────> AIService: OpenAI -> Gemini
 operator approves    │  └────> ChertAdapter | MockAdapter
                      └<── POST /api/webhooks/chert  (via tunnel)
```

1. Creating a run pre-generates the patient stream and simulates both tracks.
2. Ticks and events are written to Tiger; comparison charts read from SQL aggregations.
3. The operator approves a proposal and Chert sends the message.
4. The verified webhook stores a confirmation, Track B is re-simulated and re-persisted, and a
   WebSocket push updates the dashboard.

No workers, brokers, ORM or auth. If the database is down, charts fall back to in-memory results
under a visible "DB offline" badge and are never reported as a Tiger success.

## B. Monorepo tree

```
TriageX/
├─ backend/
│  ├─ pyproject.toml            (uv, Python 3.12)
│  ├─ app/
│  │  ├─ main.py  config.py  logging_config.py
│  │  ├─ simulation/   engine.py scenarios.py hospitals.py forecast.py
│  │  │                interventions.py metrics.py models.py
│  │  │                arrivals.py comparison.py cli.py
│  │  ├─ api/          health.py runs.py coordination.py webhooks.py ai.py ws.py
│  │  ├─ services/     run_service.py coordination_service.py safety_screen.py
│  │  ├─ integrations/ ai_base.py openai_provider.py gemini_provider.py
│  │  │                chert.py chert_mock.py
│  │  ├─ models/       (Pydantic API + AI schemas)
│  │  └─ repositories/ db.py metrics_repo.py events_repo.py
│  └─ tests/
├─ frontend/  src/{components,pages,hooks,services,types,test}
├─ db/migrations/  001_core.sql 002_metrics_hypertable.sql 003_views.sql
├─ scripts/   dev.ps1 migrate.py verify_db.py verify_chert.py
├─ docs/      plan.md assumptions.md demo-script.md
└─ README.md  .gitignore  .env.example
```

## C. Database design

| Table | Key | Notes |
|---|---|---|
| `hospitals` | `hospital_id` | name, lat/lon, synthetic capacity |
| `scenarios` | `scenario_id` | name, parameters as JSONB |
| `simulation_runs` | `run_id` (uuid) | scenario FK, seed, created_at, intervention-set hash |
| `hospital_metrics` (hypertable) | `(run_id, track, hospital_id, ts)` | `saturation_pct`, `average_wait_minutes`, `ambulance_handoff_minutes`, `confirmed_staff`, `diverted_count`, `transfer_count`, `queue_length`, `occupancy`, `staff_on_duty` |
| `staffing_requests` | `request_id` | run FK, hospital, status, `expires_at`, Chert chat id |
| `outreach_events` | `id` | request FK, recipient hash, channel, provider message id |
| `staff_confirmations` | `id` | request FK, `source` (`webhook` or `simulated`), unique on request and staff |
| `intervention_events` | `id` | run FK, type, sim effective time, `approved_by` |
| `transfer_events` | `id` | run FK, track, from, to, sim time, patient id |
| `webhook_events` | `event_id` | dedupe by primary key, signature result |
| `agent_activity` | `id` | feed items with `origin` (`live` or `synthetic`) |
| `ai_briefs` | `id` | run FK, provider, model, fallback flag, latency, JSON |

- Hypertable: `CREATE TABLE … WITH (tsdb.hypertable, tsdb.partition_column='ts')` (TimescaleDB
  2.20 or later), falling back to `create_hypertable()` on older versions.
- `ts` is simulated time anchored to a fixed date. The primary key includes `ts`.
- Index `(run_id, track, ts DESC)`. Hourly `time_bucket` rollups and a baseline-vs-TriageX
  comparison view.

## D. API design

| Method and path | Purpose |
|---|---|
| `GET /api/health`, `GET /api/integrations/health` | liveness; status of DB, OpenAI, Gemini, Chert |
| `GET /api/hospitals`, `GET /api/scenarios` | static reference data |
| `POST /api/runs` `{scenario_id, seed}` | create a run and simulate both tracks |
| `GET /api/runs/{id}` | both timelines, events, proposals |
| `GET /api/runs/{id}/comparison` | SQL-backed summary and hourly series |
| `POST /api/runs/{id}/proposals/{pid}/approve` | human approval; triggers outreach or level-loading |
| `POST /api/staffing-requests/{rid}/simulate-reply` | fallback confirmation, labeled simulated |
| `POST /api/webhooks/chert` | signed inbound event |
| `POST /api/runs/{id}/brief` | AI operational brief |
| `POST /api/runs/{id}/replay-scripted`, `POST /api/demo/reset` | demo reliability |

WebSocket `/ws` sends `feed.event`, `run.updated`, `integration.status`, `brief.ready`. Each feed
event carries `origin: live | synthetic`.

## E. Simulation specification

Implemented in Phase 2. The authoritative, detailed version is
[assumptions.md](assumptions.md). Differences from this plan, made during Phase 2:

- Three extra modules: `arrivals.py` (all randomness), `comparison.py` (two-track runs
  and deltas) and `cli.py` (command-line report).
- Separate surge spaces are not modeled. Capacity is `min(spaces, staff × 3.5)`.
- An 8-hour warm-up gives hospitals a realistic starting occupancy, identical in both
  tracks.
- When the source hospital is already diverting, active level-loading chooses a
  forecast-aware receiver, and the patient still counts as a diversion.

- **Time:** 15-minute ticks over a 24-hour horizon (96 ticks).
- **State per hospital:** treatment spaces, surge spaces, staff on duty, patients in treatment
  with remaining ticks, waiting queue, diversion flag.
- **Capacity:** `min(spaces, floor(staff × patients_per_staff))`. Extra staff cannot exceed
  physical space.
- **Arrivals:** Poisson per hospital per tick; rate is the diurnal base times the scenario
  multiplier. Each patient is fully pre-sampled (arrival tick, hospital, ambulance or walk-in,
  acuity 1–3, service duration) before either track runs.
- **Service:** a patient holds one staffed space for the pre-sampled duration.
- **Queue discipline:** acuity, then arrival time, then patient id.
- **Ambulance handoff:** the crew is held until the patient is placed. Handoff wait is placement
  tick minus arrival tick.
- **Diversion (both tracks):** saturation of 100% or more with a handoff backlog for two ticks.
  Non-critical ambulances reroute to the nearest hospital that is not diverting.
- **Staffing intervention (Track B):** each confirmation adds one staff member at the requested
  hospital, effective at request time plus 45 simulated minutes, for 4 hours.
- **Level-loading (Track B):** after approval, lower-acuity inbound ambulance patients are
  redirected up to a per-tick cap. The receiver's projected saturation must stay under 80%.
  Haversine travel time is added.
- **Forecast:** projected saturation two hours ahead from current occupancy, queue, trailing-hour
  arrival rate shaped by the diurnal profile, and mean service completion. 90% or more raises a
  proposal.
- **Metrics:** computed from the patient ledger and tick states: mean wait, peak saturation, mean
  and total handoff wait (ambulance-hours), total queue-minutes, staff utilization, transfer and
  diversion-onset counts. Nothing is hardcoded; an intervention that does not help is shown as such.
- **Determinism:** standard-library `random.Random` with sub-streams keyed by (seed, scenario,
  hospital), no wall-clock reads, stable sort keys.

Scenarios: I-85 Multi-Vehicle Pileup, Big Game Downtown, Summer Heat Wave, Respiratory Illness
Wave. Hospitals: Grady Memorial, Emory University Hospital, Emory University Hospital Midtown,
Piedmont Atlanta, Wellstar Kennestone.

## F. AI failover design

- An `AIProvider` protocol with one method, `generate(task, facts, schema)`, implemented for
  OpenAI and Gemini. `AIService` exposes `generate_operational_brief`,
  `explain_simulation_comparison`, `draft_staff_outreach_message`,
  `classify_nonemergency_message`.
- Both providers use their SDK's native Pydantic schema mode; the result is re-validated either way.
- Fallback triggers: timeout (8 seconds), 429, 5xx, connection errors, invalid structure after one
  retry.
- Auth and config errors are logged at ERROR and shown in the health panel as "primary
  misconfigured". Fallback is never silent.
- Grounding: the model receives a facts JSON and cites `metric_key` values; the server renders the
  numbers. A validator rejects any numeral in the free text that is not in the facts.
- If both fail: a deterministic template brief labeled "AI unavailable".
- Model IDs come from env vars and are checked against official docs in Phase 5.

## G. Chert integration strategy

**Verified from the official docs** (re-read the raw pages in Phase 6):

- Send: `POST https://console.trychert.com/api/v1/send` with `{phone, body}`. The response has
  `message_id` and `lead_id` (the chat id).
- Auth: `Authorization: Bearer <signing_secret>`, or `x-chert-signature: v1,<ts>,<hex>` with
  `x-chert-tenant`.
- Subscribe: `POST /api/v1/webhook-subscriptions` with `events: ["message.received"]`.
- Inbound payload: `event_id`, `event_type`, `data.chat.id`, `data.message.sender_handle`,
  `data.message.parts[].value`, `data.message.direction`.
- Signature: `X-Webhook-Signature: t=<ts>,v1=<hex>`, HMAC-SHA256 of `<ts>.<raw body>` with the
  subscription secret, 300-second tolerance.
- Delivery: at-least-once, deduplicated on `event_id`. 5xx is retried up to 3 attempts.

**Not verified:** SMS support, rate limits, dev-account limits, remaining subscription body fields
(including the URL field name), the SSE stream, and how a tenant and phone line get provisioned.

**Handling rules:**

- Outreach text always starts with `TriageX DEMO` and states it is a fictional hackathon scenario.
  Example: "TriageX DEMO: Simulated Grady surge. Are you available for a fictional 4-hour shift?
  Reply IN to confirm this demo scenario."
- Correlate `data.chat.id` to the stored `lead_id` first, then the normalized sender against the
  allowlist (senders can be Apple ID emails).
- Trim and case-fold replies; accept only `IN`.
- Requests expire after 10 minutes of wall-clock time.
- Unknown sender, expired request and duplicate events are logged and change no state.
- `MockChertAdapter` is used when credentials are absent, and the UI shows "MOCK".

## H. UI layout and demo flow

- **Top bar:** scenario selector, Reset, Replay, integration health pills, synthetic-data banner.
- **Left:** SVG map of the five hospitals projected from lat/lon (no tile server), nodes colored
  by status, transfer arcs.
- **Centre:** Parallel Futures — two synchronized charts ("Without TriageX" / "With TriageX") and
  a comparison tile row.
- **Right:** coordination feed with LIVE and SYNTHETIC badges, proposal card with Approve,
  Simulate Reply.
- **Bottom:** timeline slider, AI brief with provider badge.

**90-second demo script:**

1. 0–15s: pick I-85 Pileup and scrub forward; Grady turns red and both tracks are identical.
2. 15–30s: the forecast proposal appears and the operator approves it.
3. 30–55s: a teammate's phone receives the "TriageX DEMO" iMessage and replies IN; the feed shows
   a LIVE confirmation.
4. 55–75s: Track B recalculates, the lines diverge, the tiles update.
5. 75–90s: generate the AI brief and show the hourly comparison from Tiger.

## I. Phases

Order: 1 → 2 → 3 → 7a → 4 → 6 → 5 → 7b → 8. About 9.25 hours in full, about 6.5 with the cuts.

| Phase | Time | Stop condition | Cut if behind |
|---|---|---|---|
| 1 Setup | 0.5h | both dev servers start; pytest and vitest run | frontend lint config |
| 2 Simulation | 1.75h | simulation tests green; CLI prints a comparison | two scenarios become parameter-only variants |
| 3 Backend | 0.75h | run endpoints return both timelines; WS echoes events | WebSockets → 1-second polling |
| 7a UI skeleton | 1.0h | map, slider, charts render real API data | — |
| 4 Tiger | 0.75h | `verify_db.py` round-trips; comparison served from SQL | `transfer_events` detail rows |
| 6 Chert | 1.25h (cap 1.5h) | real IN reply moves Track B, or mock is labeled | HMAC request signing |
| 5 AI | 0.75h | brief from OpenAI; forced failure yields Gemini | outreach drafting → template |
| 7b UI finish | 1.5h | feed, brief, health, reset complete | arc animation, drill-down |
| 8 Integration | 1.0h | three clean rehearsals in a row | deployment, safety screen |

Environment variables are listed in [.env.example](../.env.example).

## J. Top risks and cut order

1. Chert live round trip (provisioning, tunnel, iMessage-only).
2. Frontend scope.
3. Honest results that look unimpressive.
4. Tiger connectivity at the venue.
5. Structured-output differences between the two AI SDKs.

Cut order: patient navigation, deployment, transfer animation, live Gemini demo, two of four
scenarios, WebSockets, HMAC request signing.

## K. Acceptance checklist

- [ ] The same seed produces byte-identical metrics on two runs.
- [ ] Track B with zero interventions equals baseline.
- [ ] `pytest` is green; any skipped integration test is listed by name.
- [ ] `verify_db.py` writes and reads a row on Tiger; `hospital_metrics` is a hypertable.
- [ ] A real iMessage reply of "IN" changes Track B within 5 seconds, or the mock is visibly labeled.
- [ ] A duplicate webhook produces exactly one confirmation.
- [ ] A forced OpenAI failure yields a Gemini brief with the fallback badge.
- [ ] Every number in the brief traces to a metric key.
- [ ] The synthetic-data banner is visible on every view; outreach text begins "TriageX DEMO".
- [ ] Reset followed by a full demo completes in under 90 seconds, three times in a row.

## L. Open questions

1. Chert: is there a tenant slug, signing secret and active phone line yet?
2. Are the opted-in reply phones iPhones?
3. Has the Tiger service been created, and on which plan?
4. Is a laptop with a tunnel acceptable for judging?
5. Agree to cut patient navigation from the MVP?
6. Do both the OpenAI and Gemini keys have credit?
