# TriageX Database (Phase 4: Tiger Data)

TriageX stores every demo run in PostgreSQL and uses TimescaleDB, which Tiger Data
provides, for the per-tick hospital time series. Every stored value is synthetic
simulation output.

## Setup

1. Create a Tiger Data service and copy its connection string.
2. Put the connection string in the repo-root `.env` file (never committed):

   ```
   DATABASE_URL=postgres://tsdbadmin:<password>@<service>.tsdb.cloud.timescale.com:<port>/tsdb?sslmode=require
   ```

3. Apply the migrations and run the end-to-end check:

   ```powershell
   cd backend
   uv run python ../scripts/migrate.py      # safe to repeat
   uv run python ../scripts/verify_db.py    # prints VERIFIED only if every check passes
   ```

4. Start the API as usual. It connects and migrates in the background, so startup
   never waits for the database.

Without `DATABASE_URL` the app runs fully in memory. In that case
`/api/integrations/health` reports the database as `not_configured`, never `ok`.

**Driver:** the app uses `pg8000`, a pure-Python PostgreSQL driver. Windows Application
Control on the demo laptop blocks psycopg's native libpq DLL.

**TLS:** `sslmode` follows libpq. `require` encrypts the connection without checking the
certificate; `verify-full` checks it.

## Schema

The schema lives in `db/migrations/` and is applied in order by
`app/repositories/migrations.py`. Each migration is recorded with a checksum in
`schema_migrations`.

| Table | Key | Contents |
|---|---|---|
| `hospitals` | `hospital_id` | Names, coordinates, synthetic capacity |
| `scenarios` | `scenario_id` | Scenario parameters, assumptions (JSONB), definition hash |
| `simulation_runs` | `run_id` | Scenario, seed, synthetic start time, baseline series digest, engine version |
| `run_versions` | `(run_id, version)` | One row per state change, recording which Track B series it shows, confirmed staff and recompute time |
| `hospital_metrics` | `(run_id, track, series_version, hospital_id, ts)` | Per-tick state for both tracks; this is the **hypertable** on `ts` |
| `comparison_metrics` | `(run_id, version, scope, metric)` | Demo-window, full-horizon and hero metrics exactly as returned to the dashboard |
| `staffing_requests` | `request_id` | Proposal, approval and effective ticks, wall-clock approval and expiry |
| `staff_confirmations` | `(request_id, staff_id)` | One per staff member; `source` is `simulated` or `webhook` |
| `intervention_events` | `(run_id, intervention_id, kind)` | Outreach approved, shift active and shift ended (plus the level-loading equivalents), in simulated and wall-clock time |
| `agent_activity` | `(run_id, seq)` | The coordination feed as last shown on the dashboard |

### How history is kept

- The baseline track is written once, as `series_version 0`, and never rewritten.
- Each recalculation of Track B is written as a new `series_version`. A run with three
  confirmations stores five series: baseline, plus Track B at 0, 1, 2 and 3
  confirmations. That is 2,400 rows.

### Time

- Simulated ticks are mapped onto a synthetic calendar day, 2026-08-15 in
  America/New_York. For I-85, tick 0 is 12:00 and the 16:30 approval is stored as
  `2026-08-15 16:30-04:00`.
- Query results are shown on that clock.

### Hypertable

- When the `timescaledb` extension is present, the migration runner converts
  `hospital_metrics` with
  `create_hypertable('hospital_metrics', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE, migrate_data => TRUE)`.
- On plain PostgreSQL, such as the local test server, the table stays an ordinary
  table and queries use `date_bin` instead of `time_bucket`.

### Tables deferred from the Phase 0 plan

| Table | Why it is deferred |
|---|---|
| `outreach_events` and `webhook_events` | Need real messages; Phase 6 (Chert) |
| `ai_briefs` | Phase 5 |
| `transfer_events` | Per-tick reroute counts are already in `hospital_metrics` (`diverted_out`, `transfers_out`), and per-patient flows can be rebuilt from the deterministic replay |

## Write path

1. After each state change (reset, approval, confirmation), the API builds an immutable
   snapshot in about 0.1 ms and returns the dashboard response immediately.
2. A single background thread writes each snapshot in one transaction.
3. Every insert is idempotent: `ON CONFLICT DO NOTHING` or an upsert. The feed table is
   replaced per run inside the same transaction, so replaying a snapshot changes
   nothing.

Failure handling:

| Failure | What happens |
|---|---|
| Database unreachable | The snapshot stays queued and is retried with backoff (0.25 s, doubling, capped at 5 s). Order is preserved. |
| Statement fails while the database is reachable | After 3 attempts the snapshot moves to a dead-letter list, so later writes keep flowing. |

Either way, the in-memory demo never depends on the database.

**Visibility:** `GET /api/db/status` shows live connectivity (cached for 3 seconds, each
check capped at 2 seconds), the server and TimescaleDB versions, whether the hypertable
exists, and the writer counters. The counters are pending, written, rows, failed
attempts, dropped and dead letters, plus the last error.

## Stored-data queries (sponsor demo)

`GET /api/runs/{run_id}/db/insights` runs these against the persisted rows and returns
each query's SQL, columns and rows. The same output is printed by `verify_db.py`.

| Query | Shows |
|---|---|
| `hourly_focus_comparison` | Focus hospital per hour: average and peak saturation, ambulance-hours waiting, staff, baseline vs TriageX |
| `ambulance_handoff_by_hospital` | Demo-window ambulance-hours and breach ticks per hospital, both tracks |
| `intervention_timeline` | Each intervention event joined to both tracks' saturation and staffing at that moment |
| `focus_recovery` | First breach and first full calm hour after it, per track (window functions) |
| `confirmation_impact_history` | Every stored version, confirmed staff, and the hero metric at that version |
| `stored_row_counts` | Rows stored for the run in each table |

Example `focus_recovery` output (I-85, seed 42, local PostgreSQL 16.2):

```
track     first_breach_at            back_to_normal_at          breach_hours
baseline  2026-08-15T16:30:00-04:00  2026-08-15T20:45:00-04:00  3.0
triagex   2026-08-15T16:30:00-04:00  2026-08-15T20:00:00-04:00  1.5
```

## Tests

`tests/test_db_persistence.py` runs against `TRIAGEX_TEST_DATABASE_URL` when set. Point
it at a Tiger Data service to exercise the hypertable path. Otherwise the tests start a
throwaway local PostgreSQL via `pgserver`, a dev dependency.

Each test uses its own schema, which is dropped afterwards. Tests never read the
developer's `DATABASE_URL`.
