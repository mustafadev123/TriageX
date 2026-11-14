"""Asynchronous, idempotent persistence of demo runs to Tiger Data.

The API never waits for the database. After each state change the route captures an
immutable `RunSnapshot` (cheap: references to immutable track results plus small copies)
and hands it to `PersistenceWriter`, whose background thread writes it in one
transaction. Every write is an upsert or insert-if-absent, so a snapshot can be retried
or replayed without duplicating rows.

Failure handling:
  - Database unreachable: the snapshot stays at the head of the queue and is retried with
    backoff until it succeeds. Order is preserved. Nothing in memory depends on it.
  - A statement keeps failing while the database is reachable: after a few attempts the
    snapshot is moved to a dead-letter list (visible in /api/db/status) so it cannot
    block later writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import ENGINE_VERSION
from app.models.dashboard import DashboardState
from app.repositories.db import CONNECTION_ERRORS, Database
from app.repositories.migrations import MigrationReport, migrate
from app.services.demo_service import Run
from app.simulation.comparison import MetricDelta
from app.simulation.hospitals import Network
from app.simulation.models import TICK_MINUTES, TrackResult
from app.simulation.scenarios import Scenario

log = logging.getLogger("triagex.persistence")

# Synthetic calendar day the simulation clock is mapped onto (America/New_York).
SIM_DATE = (2026, 8, 15)
SIM_TZ = ZoneInfo("America/New_York")
MAX_STATEMENT_ATTEMPTS = 3
MAX_QUEUE = 2000


def sim_start(scenario: Scenario) -> datetime:
    midnight = datetime(*SIM_DATE, tzinfo=SIM_TZ)
    return midnight + timedelta(minutes=scenario.start_clock_minutes)


def tick_time(scenario: Scenario, tick: int) -> datetime:
    return sim_start(scenario) + timedelta(minutes=tick * TICK_MINUTES)


@dataclass(frozen=True, slots=True)
class SeriesSnapshot:
    track: str
    series_version: int
    result: TrackResult  # immutable once produced by the engine


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    version: int
    reason: str
    recorded_at: datetime
    scenario: Scenario
    seed: int
    created_at: datetime
    network: Network
    patients_sampled: int
    baseline: TrackResult  # digests are computed in the writer thread, not the request
    triagex: TrackResult
    triagex_series_version: int
    confirmed_staff: int
    recompute_ms: float | None
    series: tuple[SeriesSnapshot, ...]
    comparison_rows: tuple[tuple[str, str, float, float, float, float | None], ...]
    staffing_requests: tuple[dict, ...]
    confirmations: tuple[dict, ...]
    intervention_events: tuple[dict, ...]
    activity: tuple[dict, ...]


def series_digest(result: TrackResult) -> str:
    """SHA-256 over the stored per-tick series (about 1 ms; the full diagnostics digest
    takes about 30 ms and would compete with live requests for the GIL)."""
    h = hashlib.sha256()
    for tick in result.ticks:
        for x in tick:
            h.update(
                f"{x.tick},{x.hospital_id},{x.status.value},{x.saturation_pct!r},{x.occupied},"
                f"{x.queue_length},{x.ambulance_queue_length},{x.staff_on_duty},"
                f"{x.staffed_capacity},{x.mean_current_wait_minutes!r},"
                f"{x.forecast_saturation_pct!r},{x.diverting:d},{x.diverted_out},"
                f"{x.transfers_out};".encode()
            )
    return h.hexdigest()


def _delta_rows(scope: str, deltas: dict[str, MetricDelta]):
    return tuple(
        (scope, name, d.baseline, d.triagex, d.delta, d.pct_change) for name, d in deltas.items()
    )


def build_snapshot(run: Run, state: DashboardState, reason: str) -> RunSnapshot:
    """Capture everything to persist for this version. Call while handling the request."""
    c, sc = run.comparison, run.scenario
    series_version = len(run.recompute_history_ms)
    series = [SeriesSnapshot("triagex", series_version, c.triagex)]
    if reason in ("run_created", "reset"):
        series.insert(0, SeriesSnapshot("baseline", 0, run.baseline))

    hero = [
        ("hero", m.key, m.baseline, m.triagex, m.delta, m.pct_change)
        for m in (state.impact.primary, *state.impact.secondary)
    ]
    rec = state.impact.recovery
    if rec.minutes_sooner is not None:
        hero.append(("hero", "recovery_minutes_sooner", 0.0, float(rec.minutes_sooner),
                     float(rec.minutes_sooner), None))  # fmt: skip
    hero.append(("hero", "focus_breach_hours", rec.baseline_breach_hours,
                 rec.triagex_breach_hours, rec.triagex_breach_hours - rec.baseline_breach_hours,
                 None))  # fmt: skip

    requests, confirmations, events = [], [], []
    for r in run.staffing_requests.values():
        iv = r.intervention()
        requests.append({
            "request_id": r.request_id, "run_id": run.run_id, "proposal_id": r.proposal_id,
            "hospital_id": r.hospital_id, "requested_staff": r.requested_staff,
            "proposal_tick": r.proposal_tick, "approved_tick": r.approved_tick,
            "effective_tick": iv.effective_tick, "end_tick": iv.end_tick,
            "approved_at": r.approved_at, "expires_at": r.expires_at, "operator": r.operator,
        })  # fmt: skip
        for conf in r.confirmations:
            confirmations.append({
                "request_id": r.request_id, "staff_id": conf.staff_id,
                "display_name": conf.display_name, "source": conf.source,
                "received_at": conf.received_at, "recompute_ms": conf.recompute_ms,
            })  # fmt: skip
        base = {"intervention_id": r.request_id, "hospital_id": r.hospital_id}
        events.append({**base, "kind": "outreach_approved", "tick": r.approved_tick,
                       "wall_time": r.approved_at,
                       "detail": {"proposal_tick": r.proposal_tick, "operator": r.operator,
                                  "requested_staff": r.requested_staff}})  # fmt: skip
        if r.confirmed_staff:
            detail = {"confirmed_staff": r.confirmed_staff,
                      "sources": sorted({x.source for x in r.confirmations})}  # fmt: skip
            events.append({**base, "kind": "shift_active", "tick": iv.effective_tick,
                           "wall_time": None, "detail": detail})  # fmt: skip
            events.append({**base, "kind": "shift_ended", "tick": iv.end_tick,
                           "wall_time": None, "detail": detail})  # fmt: skip
    for action in run.actions:
        if action.kind != "level_loading_approved":
            continue
        iv = next(iv for iv in run.level_loading.values() if iv.approved_tick == action.tick
                  and iv.hospital_id == action.hospital_id)  # fmt: skip
        base = {"intervention_id": iv.id, "hospital_id": iv.hospital_id}
        events.append(
            {
                **base,
                "kind": "level_loading_approved",
                "tick": iv.approved_tick,
                "wall_time": action.wall_time,
                "detail": {"operator": action.operator},
            }
        )
        events.append({**base, "kind": "level_loading_active", "tick": iv.effective_tick,
                       "wall_time": None, "detail": {}})  # fmt: skip
        events.append({**base, "kind": "level_loading_ended", "tick": iv.end_tick,
                       "wall_time": None, "detail": {}})  # fmt: skip

    activity = tuple(
        {
            "seq": i,
            "event_id": e.id,
            "tick": e.tick,
            "title": e.title,
            "detail": e.detail,
            "kind": e.kind,
            "track": e.track,
            "origin": e.origin,
            "severity": e.severity,
            "hospital_id": e.hospital_id,
            "wall_time": e.wall_time,
        }  # fmt: skip
        for i, e in enumerate(state.feed)
    )
    return RunSnapshot(
        run_id=run.run_id,
        version=run.version,
        reason=reason,
        recorded_at=datetime.now(UTC),
        scenario=sc,
        seed=run.seed,
        created_at=run.created_at,
        network=run.network,
        patients_sampled=len(run.patients),
        baseline=run.baseline,
        triagex=c.triagex,
        triagex_series_version=series_version,
        confirmed_staff=sum(r.confirmed_staff for r in run.staffing_requests.values()),
        recompute_ms=run.last_recompute_ms,
        series=tuple(series),
        comparison_rows=(
            *_delta_rows("demo_window", c.window_deltas),
            *_delta_rows("full_horizon", c.deltas),
            *hero,
        ),
        staffing_requests=tuple(requests),
        confirmations=tuple(confirmations),
        intervention_events=tuple(events),
        activity=activity,
    )


# --- writing -------------------------------------------------------------------------------


# Each snapshot is written with two statements in one transaction (plus BEGIN/COMMIT), so
# the cost is a handful of network round trips regardless of how many rows it carries.
# Against Tiger Data (~230 ms per round trip from the demo laptop) one statement per row
# took ~20 s per snapshot.

_CORE_SQL = """
WITH
h AS (
    INSERT INTO hospitals (hospital_id, name, short_name, lat, lon, treatment_spaces,
                           base_staff, patients_per_staff)
    SELECT * FROM unnest(CAST(:h_id AS text[]), CAST(:h_name AS text[]),
                         CAST(:h_short AS text[]), CAST(:h_lat AS float8[]),
                         CAST(:h_lon AS float8[]), CAST(:h_spaces AS int[]),
                         CAST(:h_staff AS int[]), CAST(:h_pps AS float8[]))
    ON CONFLICT (hospital_id) DO UPDATE SET name = EXCLUDED.name,
        short_name = EXCLUDED.short_name, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
        treatment_spaces = EXCLUDED.treatment_spaces, base_staff = EXCLUDED.base_staff,
        patients_per_staff = EXCLUDED.patients_per_staff
    RETURNING 1
),
s AS (
    INSERT INTO scenarios (scenario_id, name, description, start_clock_minutes,
        surge_onset_tick, demo_window_start_tick, demo_window_ticks, horizon_ticks,
        focus_hospital_id, assumptions, definition_sha256)
    VALUES (:sc_id, :sc_name, :sc_description, :sc_start, :sc_onset, :sc_ws, :sc_wt,
            :sc_horizon, :sc_focus, CAST(:sc_assumptions AS jsonb), :sc_sha)
    ON CONFLICT (scenario_id) DO UPDATE SET name = EXCLUDED.name,
        description = EXCLUDED.description, assumptions = EXCLUDED.assumptions,
        definition_sha256 = EXCLUDED.definition_sha256
    RETURNING 1
),
r AS (
    INSERT INTO simulation_runs (run_id, scenario_id, seed, created_at, sim_start,
        tick_minutes, patients_sampled, baseline_digest, engine_version)
    VALUES (:run_id, :sc_id, :seed, CAST(:created_at AS timestamptz),
            CAST(:sim_start AS timestamptz), :tick_minutes, :patients, :baseline_digest,
            :engine)
    ON CONFLICT (run_id) DO NOTHING
    RETURNING 1
),
v AS (
    INSERT INTO run_versions (run_id, version, reason, recorded_at, triagex_series_version,
        triagex_digest, confirmed_staff, recompute_ms)
    VALUES (:run_id, :version, :reason, CAST(:recorded_at AS timestamptz), :series_version,
            :triagex_digest, :confirmed_staff, CAST(:recompute_ms AS float8))
    ON CONFLICT (run_id, version) DO NOTHING
    RETURNING 1
),
c AS (
    INSERT INTO comparison_metrics (run_id, version, scope, metric, baseline, triagex, delta,
                                    pct_change)
    SELECT :run_id, :version, u.* FROM unnest(CAST(:c_scope AS text[]),
        CAST(:c_metric AS text[]), CAST(:c_base AS float8[]), CAST(:c_tri AS float8[]),
        CAST(:c_delta AS float8[]), CAST(:c_pct AS float8[])) AS u
    ON CONFLICT DO NOTHING
    RETURNING 1
),
q AS (
    INSERT INTO staffing_requests (request_id, run_id, proposal_id, hospital_id,
        requested_staff, proposal_tick, approved_tick, effective_tick, end_tick,
        approved_at, expires_at, operator)
    SELECT u.request_id, :run_id, u.proposal_id, u.hospital_id, u.requested_staff,
           u.proposal_tick, u.approved_tick, u.effective_tick, u.end_tick,
           u.approved_at::timestamptz, u.expires_at::timestamptz, u.operator
    FROM unnest(CAST(:q_id AS text[]), CAST(:q_proposal AS text[]),
                CAST(:q_hospital AS text[]), CAST(:q_requested AS int[]),
                CAST(:q_proposal_tick AS int[]), CAST(:q_approved_tick AS int[]),
                CAST(:q_effective_tick AS int[]), CAST(:q_end_tick AS int[]),
                CAST(:q_approved_at AS text[]), CAST(:q_expires_at AS text[]),
                CAST(:q_operator AS text[]))
        AS u(request_id, proposal_id, hospital_id, requested_staff, proposal_tick,
             approved_tick, effective_tick, end_tick, approved_at, expires_at, operator)
    ON CONFLICT (request_id) DO NOTHING
    RETURNING 1
),
f AS (
    INSERT INTO staff_confirmations (request_id, staff_id, display_name, source,
                                     received_at, recompute_ms)
    SELECT u.request_id, u.staff_id, u.display_name, u.source, u.received_at::timestamptz,
           u.recompute_ms
    FROM unnest(CAST(:f_request AS text[]), CAST(:f_staff AS text[]),
                CAST(:f_name AS text[]), CAST(:f_source AS text[]),
                CAST(:f_received AS text[]), CAST(:f_ms AS float8[]))
        AS u(request_id, staff_id, display_name, source, received_at, recompute_ms)
    ON CONFLICT (request_id, staff_id) DO NOTHING
    RETURNING 1
),
e AS (
    INSERT INTO intervention_events (run_id, intervention_id, kind, hospital_id, tick,
                                     sim_time, wall_time, detail)
    SELECT :run_id, u.iid, u.kind, u.hid, u.tick, u.sim_time::timestamptz,
           u.wall_time::timestamptz, u.detail::jsonb
    FROM unnest(CAST(:e_iid AS text[]), CAST(:e_kind AS text[]), CAST(:e_hid AS text[]),
                CAST(:e_tick AS int[]), CAST(:e_sim AS text[]), CAST(:e_wall AS text[]),
                CAST(:e_detail AS text[]))
        AS u(iid, kind, hid, tick, sim_time, wall_time, detail)
    ON CONFLICT (run_id, intervention_id, kind) DO UPDATE SET detail = EXCLUDED.detail
    RETURNING 1
),
d AS (
    DELETE FROM agent_activity WHERE run_id = :run_id AND seq >= :activity_count
    RETURNING 1
),
a AS (
    INSERT INTO agent_activity (run_id, seq, event_id, tick, sim_time, title, detail, kind,
                                track, origin, severity, hospital_id, wall_time)
    SELECT :run_id, u.seq, u.event_id, u.tick, u.sim_time::timestamptz, u.title, u.detail,
           u.kind, u.track, u.origin, u.severity, u.hospital_id, u.wall_time::timestamptz
    FROM unnest(CAST(:a_seq AS int[]), CAST(:a_event AS text[]), CAST(:a_tick AS int[]),
                CAST(:a_sim AS text[]), CAST(:a_title AS text[]), CAST(:a_detail AS text[]),
                CAST(:a_kind AS text[]), CAST(:a_track AS text[]), CAST(:a_origin AS text[]),
                CAST(:a_severity AS text[]), CAST(:a_hospital AS text[]),
                CAST(:a_wall AS text[]))
        AS u(seq, event_id, tick, sim_time, title, detail, kind, track, origin, severity,
             hospital_id, wall_time)
    ON CONFLICT (run_id, seq) DO UPDATE SET event_id = EXCLUDED.event_id,
        tick = EXCLUDED.tick, sim_time = EXCLUDED.sim_time, title = EXCLUDED.title,
        detail = EXCLUDED.detail, kind = EXCLUDED.kind, track = EXCLUDED.track,
        origin = EXCLUDED.origin, severity = EXCLUDED.severity,
        hospital_id = EXCLUDED.hospital_id, wall_time = EXCLUDED.wall_time
    RETURNING 1
)
SELECT (SELECT count(*) FROM r) + (SELECT count(*) FROM v) + (SELECT count(*) FROM c)
     + (SELECT count(*) FROM q) + (SELECT count(*) FROM f) + (SELECT count(*) FROM e)
     + (SELECT count(*) FROM a)
     + 0 * ((SELECT count(*) FROM h) + (SELECT count(*) FROM s) + (SELECT count(*) FROM d))
"""

_METRICS_SQL = """
INSERT INTO hospital_metrics (ts, run_id, track, series_version, hospital_id, tick, status,
    saturation_pct, occupied, queue_length, ambulances_waiting, staff_on_duty,
    staffed_capacity, mean_wait_minutes, forecast_saturation_pct, diverting, diverted_out,
    transfers_out)
SELECT u.ts::timestamptz, :run_id, u.track, u.series_version, u.hospital_id, u.tick, u.status,
       u.saturation_pct, u.occupied, u.queue_length, u.ambulances_waiting, u.staff_on_duty,
       u.staffed_capacity, u.mean_wait_minutes, u.forecast_saturation_pct, u.diverting,
       u.diverted_out, u.transfers_out
FROM unnest(CAST(:ts AS text[]), CAST(:track AS text[]), CAST(:series_version AS int[]),
            CAST(:hospital_id AS text[]), CAST(:tick AS int[]), CAST(:status AS text[]),
            CAST(:saturation_pct AS float8[]), CAST(:occupied AS int[]),
            CAST(:queue_length AS int[]), CAST(:ambulances_waiting AS int[]),
            CAST(:staff_on_duty AS int[]), CAST(:staffed_capacity AS int[]),
            CAST(:mean_wait_minutes AS float8[]), CAST(:forecast_saturation_pct AS float8[]),
            CAST(:diverting AS bool[]), CAST(:diverted_out AS int[]),
            CAST(:transfers_out AS int[]))
    AS u(ts, track, series_version, hospital_id, tick, status, saturation_pct, occupied,
         queue_length, ambulances_waiting, staff_on_duty, staffed_capacity, mean_wait_minutes,
         forecast_saturation_pct, diverting, diverted_out, transfers_out)
ON CONFLICT DO NOTHING
RETURNING 1
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _metric_columns(snap: RunSnapshot) -> dict[str, list]:
    keys = (
        "ts", "track", "series_version", "hospital_id", "tick", "status", "saturation_pct",
        "occupied", "queue_length", "ambulances_waiting", "staff_on_duty", "staffed_capacity",
        "mean_wait_minutes", "forecast_saturation_pct", "diverting", "diverted_out",
        "transfers_out",
    )  # fmt: skip
    cols: dict[str, list] = {k: [] for k in keys}
    for s in snap.series:
        for row in (r for tick in s.result.ticks for r in tick):
            cols["ts"].append(tick_time(snap.scenario, row.tick).isoformat())
            cols["track"].append(s.track)
            cols["series_version"].append(s.series_version)
            cols["hospital_id"].append(row.hospital_id)
            cols["tick"].append(row.tick)
            cols["status"].append(row.status.value)
            cols["saturation_pct"].append(float(row.saturation_pct))
            cols["occupied"].append(row.occupied)
            cols["queue_length"].append(row.queue_length)
            cols["ambulances_waiting"].append(row.ambulance_queue_length)
            cols["staff_on_duty"].append(row.staff_on_duty)
            cols["staffed_capacity"].append(row.staffed_capacity)
            cols["mean_wait_minutes"].append(float(row.mean_current_wait_minutes))
            cols["forecast_saturation_pct"].append(float(row.forecast_saturation_pct))
            cols["diverting"].append(row.diverting)
            cols["diverted_out"].append(row.diverted_out)
            cols["transfers_out"].append(row.transfers_out)
    return cols


def _core_params(snap: RunSnapshot) -> dict:
    sc, hs = snap.scenario, snap.network.hospitals
    definition = json.dumps(
        {
            "bursts": [repr(b) for b in sc.bursts],
            "windows": [repr(w) for w in sc.demand_windows],
            "service_multiplier": sc.service_multiplier,
            "ambulance_delta": sc.ambulance_fraction_delta,
            "walk_in_mix": sc.walk_in_acuity_mix,
        },
        sort_keys=True,
    )
    cmp = snap.comparison_rows
    reqs = snap.staffing_requests
    confs = snap.confirmations
    events = snap.intervention_events
    acts = snap.activity
    return {
        "h_id": [h.id for h in hs],
        "h_name": [h.name for h in hs],
        "h_short": [h.short_name for h in hs],
        "h_lat": [h.lat for h in hs],
        "h_lon": [h.lon for h in hs],
        "h_spaces": [h.treatment_spaces for h in hs],
        "h_staff": [h.base_staff for h in hs],
        "h_pps": [float(h.patients_per_staff) for h in hs],
        "sc_id": sc.id,
        "sc_name": sc.name,
        "sc_description": sc.description,
        "sc_start": sc.start_clock_minutes,
        "sc_onset": sc.surge_onset_tick,
        "sc_ws": sc.demo_window_start_tick,
        "sc_wt": sc.demo_window_ticks,
        "sc_horizon": sc.horizon_ticks,
        "sc_focus": sc.focus_hospital_id,
        "sc_assumptions": json.dumps(list(sc.assumptions)),
        "sc_sha": hashlib.sha256(definition.encode()).hexdigest(),
        "run_id": snap.run_id,
        "seed": snap.seed,
        "created_at": _iso(snap.created_at),
        "sim_start": _iso(sim_start(sc)),
        "tick_minutes": TICK_MINUTES,
        "patients": snap.patients_sampled,
        "baseline_digest": series_digest(snap.baseline),
        "engine": ENGINE_VERSION,
        "version": snap.version,
        "reason": snap.reason,
        "recorded_at": _iso(snap.recorded_at),
        "series_version": snap.triagex_series_version,
        "triagex_digest": series_digest(snap.triagex),
        "confirmed_staff": snap.confirmed_staff,
        "recompute_ms": snap.recompute_ms,
        "c_scope": [r[0] for r in cmp],
        "c_metric": [r[1] for r in cmp],
        "c_base": [float(r[2]) for r in cmp],
        "c_tri": [float(r[3]) for r in cmp],
        "c_delta": [float(r[4]) for r in cmp],
        "c_pct": [None if r[5] is None else float(r[5]) for r in cmp],
        "q_id": [r["request_id"] for r in reqs],
        "q_proposal": [r["proposal_id"] for r in reqs],
        "q_hospital": [r["hospital_id"] for r in reqs],
        "q_requested": [r["requested_staff"] for r in reqs],
        "q_proposal_tick": [r["proposal_tick"] for r in reqs],
        "q_approved_tick": [r["approved_tick"] for r in reqs],
        "q_effective_tick": [r["effective_tick"] for r in reqs],
        "q_end_tick": [r["end_tick"] for r in reqs],
        "q_approved_at": [_iso(r["approved_at"]) for r in reqs],
        "q_expires_at": [_iso(r["expires_at"]) for r in reqs],
        "q_operator": [r["operator"] for r in reqs],
        "f_request": [c["request_id"] for c in confs],
        "f_staff": [c["staff_id"] for c in confs],
        "f_name": [c["display_name"] for c in confs],
        "f_source": [c["source"] for c in confs],
        "f_received": [_iso(c["received_at"]) for c in confs],
        "f_ms": [float(c["recompute_ms"]) for c in confs],
        "e_iid": [e["intervention_id"] for e in events],
        "e_kind": [e["kind"] for e in events],
        "e_hid": [e["hospital_id"] for e in events],
        "e_tick": [e["tick"] for e in events],
        "e_sim": [tick_time(sc, e["tick"]).isoformat() for e in events],
        "e_wall": [_iso(e["wall_time"]) for e in events],
        "e_detail": [json.dumps(e["detail"], sort_keys=True) for e in events],
        "activity_count": len(acts),
        "a_seq": [a["seq"] for a in acts],
        "a_event": [a["event_id"] for a in acts],
        "a_tick": [a["tick"] for a in acts],
        "a_sim": [tick_time(sc, a["tick"]).isoformat() for a in acts],
        "a_title": [a["title"] for a in acts],
        "a_detail": [a["detail"] for a in acts],
        "a_kind": [a["kind"] for a in acts],
        "a_track": [a["track"] for a in acts],
        "a_origin": [a["origin"] for a in acts],
        "a_severity": [a["severity"] for a in acts],
        "a_hospital": [a["hospital_id"] for a in acts],
        "a_wall": [_iso(a["wall_time"]) for a in acts],
    }


def write_snapshot(con, snap: RunSnapshot) -> int:
    """Write one snapshot inside the caller's transaction. Returns rows inserted/upserted."""
    written = int(con.run(_CORE_SQL, **_core_params(snap))[0][0])
    if snap.series:
        written += len(con.run(_METRICS_SQL, run_id=snap.run_id, **_metric_columns(snap)))
    return written


@dataclass(slots=True)
class WriterStatus:
    enabled: bool
    connected: bool = False
    migrated: bool = False
    migration: MigrationReport | None = None
    pending: int = 0
    snapshots_written: int = 0
    events_written: int = 0  # outreach and webhook rows
    rows_written: int = 0
    failed_attempts: int = 0
    dropped: int = 0
    dead_letters: list[str] = field(default_factory=list)
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None
    last_write_ms: float | None = None


class PersistenceWriter:
    """Single background thread that drains snapshots in order."""

    def __init__(
        self,
        db: Database | None,
        *,
        retry_initial_s: float = 0.25,
        retry_max_s: float = 5.0,
        auto_migrate: bool = True,
    ):
        self.db = db
        self.auto_migrate = auto_migrate
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self._queue: deque[RunSnapshot] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._busy = False
        self.status = WriterStatus(enabled=db is not None)

    @property
    def enabled(self) -> bool:
        return self.db is not None

    # --- producer side (request thread) ------------------------------------------------
    def submit(self, snapshot: RunSnapshot) -> bool:
        """Queue a snapshot. Never blocks on the database and never raises."""
        if not self.enabled:
            return False
        with self._cond:
            if len(self._queue) >= MAX_QUEUE:
                self.status.dropped += 1
                log.error(
                    "persistence queue full; snapshot dropped",
                    extra={"run_id": snapshot.run_id, "version": snapshot.version},
                )
                return False
            self._queue.append(snapshot)
            self.status.pending = len(self._queue)
            self._cond.notify()
        return True

    def pending_for(self, run_id: str) -> int:
        with self._cond:
            return sum(1 for s in self._queue if s.run_id == run_id)

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every queued snapshot is written (tests and scripts)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
        return True

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="triagex-persistence", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    # --- worker --------------------------------------------------------------------------
    def _record_error(self, exc: BaseException) -> None:
        self.status.failed_attempts += 1
        self.status.last_error = f"{type(exc).__name__}: {exc}"[:500]
        self.status.last_error_at = datetime.now(UTC)

    def _ensure_ready(self) -> None:
        if not self.status.migrated:
            if self.auto_migrate:
                self.status.migration = migrate(self.db)
            self.status.migrated = True
        self.status.connected = True

    def _run(self) -> None:
        delay = self.retry_initial_s
        statement_failures = 0
        while True:
            with self._cond:
                while not self._queue and not self._stopping and self.status.migrated:
                    self._cond.wait()
                if self._stopping:
                    return
                snapshot = self._queue[0] if self._queue else None
                self._busy = snapshot is not None
            try:
                self._ensure_ready()
                if snapshot is not None:
                    started = time.perf_counter()
                    is_event = hasattr(snapshot, "write")  # messaging rows (Phase 6)
                    with self.db.transaction() as con:
                        rows = snapshot.write(con) if is_event else write_snapshot(con, snapshot)
                    with self._cond:
                        self._queue.popleft()
                        self.status.pending = len(self._queue)
                        if is_event:
                            self.status.events_written += 1
                        else:
                            self.status.snapshots_written += 1
                        self.status.rows_written += rows
                        self.status.last_success_at = datetime.now(UTC)
                        self.status.last_write_ms = round((time.perf_counter() - started) * 1000, 2)
                        self._busy = False
                        self._cond.notify_all()
                delay, statement_failures = self.retry_initial_s, 0
            except CONNECTION_ERRORS as exc:
                self.status.connected = False
                self._record_error(exc)
                log.warning("database unavailable; will retry", extra={"error": str(exc)})
                self._sleep(delay)
                delay = min(delay * 2, self.retry_max_s)
            except Exception as exc:  # statement-level failure while connected
                self._record_error(exc)
                statement_failures += 1
                log.error("persistence statement failed", exc_info=True)
                if snapshot is not None and statement_failures >= MAX_STATEMENT_ATTEMPTS:
                    with self._cond:
                        self._queue.popleft()
                        self.status.pending = len(self._queue)
                        self.status.dead_letters.append(
                            f"{snapshot.run_id} v{snapshot.version}: {self.status.last_error}"
                        )
                        del self.status.dead_letters[:-50]
                        self._busy = False
                        self._cond.notify_all()
                    statement_failures = 0
                else:
                    self._sleep(delay)
            finally:
                with self._cond:
                    if not self._queue:
                        self._busy = False
                        self._cond.notify_all()

    def _sleep(self, seconds: float) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()
            if not self._stopping:
                self._cond.wait(seconds)
