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


def _insert_series(con, snap: RunSnapshot, s: SeriesSnapshot) -> int:
    cols: dict[str, list] = {k: [] for k in (
        "ts", "hospital_id", "tick", "status", "saturation_pct", "occupied", "queue_length",
        "ambulances_waiting", "staff_on_duty", "staffed_capacity", "mean_wait_minutes",
        "forecast_saturation_pct", "diverting", "diverted_out", "transfers_out")}  # fmt: skip
    for row in (r for tick in s.result.ticks for r in tick):
        cols["ts"].append(tick_time(snap.scenario, row.tick).isoformat())
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
    rows = con.run(
        """
        INSERT INTO hospital_metrics (ts, run_id, track, series_version, hospital_id, tick,
            status, saturation_pct, occupied, queue_length, ambulances_waiting, staff_on_duty,
            staffed_capacity, mean_wait_minutes, forecast_saturation_pct, diverting,
            diverted_out, transfers_out)
        SELECT u.ts::timestamptz, :run_id, :track, :series_version, u.hospital_id, u.tick,
            u.status, u.saturation_pct, u.occupied, u.queue_length, u.ambulances_waiting,
            u.staff_on_duty, u.staffed_capacity, u.mean_wait_minutes, u.forecast_saturation_pct,
            u.diverting, u.diverted_out, u.transfers_out
        FROM unnest(CAST(:ts AS text[]), CAST(:hospital_id AS text[]), CAST(:tick AS int[]),
                    CAST(:status AS text[]), CAST(:saturation_pct AS float8[]),
                    CAST(:occupied AS int[]), CAST(:queue_length AS int[]),
                    CAST(:ambulances_waiting AS int[]), CAST(:staff_on_duty AS int[]),
                    CAST(:staffed_capacity AS int[]), CAST(:mean_wait_minutes AS float8[]),
                    CAST(:forecast_saturation_pct AS float8[]), CAST(:diverting AS bool[]),
                    CAST(:diverted_out AS int[]), CAST(:transfers_out AS int[]))
            AS u(ts, hospital_id, tick, status, saturation_pct, occupied, queue_length,
                 ambulances_waiting, staff_on_duty, staffed_capacity, mean_wait_minutes,
                 forecast_saturation_pct, diverting, diverted_out, transfers_out)
        ON CONFLICT DO NOTHING
        RETURNING 1
        """,
        run_id=snap.run_id,
        track=s.track,
        series_version=s.series_version,
        **cols,
    )
    return len(rows)


def write_snapshot(con, snap: RunSnapshot) -> int:
    """Write one snapshot inside the caller's transaction. Returns rows inserted."""
    sc, written = snap.scenario, 0
    for h in snap.network.hospitals:
        con.run(
            """INSERT INTO hospitals (hospital_id, name, short_name, lat, lon, treatment_spaces,
                   base_staff, patients_per_staff)
               VALUES (:id, :name, :short, :lat, :lon, :spaces, :staff, :pps)
               ON CONFLICT (hospital_id) DO UPDATE SET name = EXCLUDED.name,
                   short_name = EXCLUDED.short_name, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                   treatment_spaces = EXCLUDED.treatment_spaces, base_staff = EXCLUDED.base_staff,
                   patients_per_staff = EXCLUDED.patients_per_staff""",
            id=h.id, name=h.name, short=h.short_name, lat=h.lat, lon=h.lon,
            spaces=h.treatment_spaces, staff=h.base_staff, pps=h.patients_per_staff,
        )  # fmt: skip
    definition = json.dumps(
        {"bursts": [repr(b) for b in sc.bursts], "windows": [repr(w) for w in sc.demand_windows],
         "service_multiplier": sc.service_multiplier,
         "ambulance_delta": sc.ambulance_fraction_delta,
         "walk_in_mix": sc.walk_in_acuity_mix}, sort_keys=True)  # fmt: skip
    con.run(
        """INSERT INTO scenarios (scenario_id, name, description, start_clock_minutes,
               surge_onset_tick, demo_window_start_tick, demo_window_ticks, horizon_ticks,
               focus_hospital_id, assumptions, definition_sha256)
           VALUES (:id, :name, :description, :start, :onset, :ws, :wt, :horizon, :focus,
                   CAST(:assumptions AS jsonb), :sha)
           ON CONFLICT (scenario_id) DO UPDATE SET name = EXCLUDED.name,
               description = EXCLUDED.description, assumptions = EXCLUDED.assumptions,
               definition_sha256 = EXCLUDED.definition_sha256""",
        id=sc.id, name=sc.name, description=sc.description, start=sc.start_clock_minutes,
        onset=sc.surge_onset_tick, ws=sc.demo_window_start_tick, wt=sc.demo_window_ticks,
        horizon=sc.horizon_ticks, focus=sc.focus_hospital_id,
        assumptions=json.dumps(list(sc.assumptions)),
        sha=hashlib.sha256(definition.encode()).hexdigest(),
    )  # fmt: skip
    written += len(con.run(
        """INSERT INTO simulation_runs (run_id, scenario_id, seed, created_at, sim_start,
               tick_minutes, patients_sampled, baseline_digest, engine_version)
           VALUES (:run_id, :scenario_id, :seed, :created_at, :sim_start, :tick_minutes,
                   :patients, :digest, :engine)
           ON CONFLICT (run_id) DO NOTHING RETURNING 1""",
        run_id=snap.run_id, scenario_id=sc.id, seed=snap.seed, created_at=snap.created_at,
        sim_start=sim_start(sc), tick_minutes=TICK_MINUTES, patients=snap.patients_sampled,
        digest=series_digest(snap.baseline), engine=ENGINE_VERSION,
    ))  # fmt: skip
    written += len(con.run(
        """INSERT INTO run_versions (run_id, version, reason, recorded_at,
               triagex_series_version, triagex_digest, confirmed_staff, recompute_ms)
           VALUES (:run_id, :version, :reason, :recorded_at, :sv, :digest, :staff, :ms)
           ON CONFLICT (run_id, version) DO NOTHING RETURNING 1""",
        run_id=snap.run_id, version=snap.version, reason=snap.reason,
        recorded_at=snap.recorded_at, sv=snap.triagex_series_version,
        digest=series_digest(snap.triagex), staff=snap.confirmed_staff, ms=snap.recompute_ms,
    ))  # fmt: skip
    for s in snap.series:
        written += _insert_series(con, snap, s)
    for scope, metric, base, tri, delta, pct in snap.comparison_rows:
        written += len(con.run(
            """INSERT INTO comparison_metrics (run_id, version, scope, metric, baseline,
                   triagex, delta, pct_change)
               VALUES (:run_id, :version, :scope, :metric, :b, :t, :d, :p)
               ON CONFLICT DO NOTHING RETURNING 1""",
            run_id=snap.run_id, version=snap.version, scope=scope, metric=metric,
            b=float(base), t=float(tri), d=float(delta),
            p=None if pct is None else float(pct),
        ))  # fmt: skip
    for r in snap.staffing_requests:
        written += len(
            con.run(
                """INSERT INTO staffing_requests (request_id, run_id, proposal_id, hospital_id,
                   requested_staff, proposal_tick, approved_tick, effective_tick, end_tick,
                   approved_at, expires_at, operator)
               VALUES (:request_id, :run_id, :proposal_id, :hospital_id, :requested_staff,
                   :proposal_tick, :approved_tick, :effective_tick, :end_tick, :approved_at,
                   :expires_at, :operator)
               ON CONFLICT (request_id) DO NOTHING RETURNING 1""",
                **r,
            )
        )
    for conf in snap.confirmations:
        written += len(
            con.run(
                """INSERT INTO staff_confirmations (request_id, staff_id, display_name, source,
                   received_at, recompute_ms)
               VALUES (:request_id, :staff_id, :display_name, :source, :received_at,
                   :recompute_ms)
               ON CONFLICT (request_id, staff_id) DO NOTHING RETURNING 1""",
                **conf,
            )
        )
    for e in snap.intervention_events:
        written += len(con.run(
            """INSERT INTO intervention_events (run_id, intervention_id, kind, hospital_id,
                   tick, sim_time, wall_time, detail)
               VALUES (:run_id, :iid, :kind, :hid, :tick, :sim_time, :wall_time,
                   CAST(:detail AS jsonb))
               ON CONFLICT (run_id, intervention_id, kind) DO UPDATE
                   SET detail = EXCLUDED.detail
               RETURNING 1""",
            run_id=snap.run_id, iid=e["intervention_id"], kind=e["kind"],
            hid=e["hospital_id"], tick=e["tick"], sim_time=tick_time(sc, e["tick"]),
            wall_time=e["wall_time"], detail=json.dumps(e["detail"], sort_keys=True),
        ))  # fmt: skip
    con.run("DELETE FROM agent_activity WHERE run_id = :run_id", run_id=snap.run_id)
    for a in snap.activity:
        written += len(con.run(
            """INSERT INTO agent_activity (run_id, seq, event_id, tick, sim_time, title, detail,
                   kind, track, origin, severity, hospital_id, wall_time)
               VALUES (:run_id, :seq, :event_id, :tick, :sim_time, :title, :detail, :kind,
                   :track, :origin, :severity, :hospital_id, :wall_time)
               RETURNING 1""",
            run_id=snap.run_id, sim_time=tick_time(sc, a["tick"]), **a,
        ))  # fmt: skip
    return written


@dataclass(slots=True)
class WriterStatus:
    enabled: bool
    connected: bool = False
    migrated: bool = False
    migration: MigrationReport | None = None
    pending: int = 0
    snapshots_written: int = 0
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
                    with self.db.transaction() as con:
                        rows = write_snapshot(con, snapshot)
                    with self._cond:
                        self._queue.popleft()
                        self.status.pending = len(self._queue)
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
