"""Phase 4: Tiger Data persistence against a real PostgreSQL server.

Runs against TRIAGEX_TEST_DATABASE_URL when set (for example a Tiger Data service), or a
throwaway local PostgreSQL otherwise. TimescaleDB-specific behaviour (hypertables,
time_bucket) is exercised only when the server has the extension.
"""

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.db import Database, parse_database_url
from app.repositories.migrations import hypertable_statement, migrate, migration_files
from app.repositories.persistence import (
    PersistenceWriter,
    build_snapshot,
    series_digest,
    tick_time,
    write_snapshot,
)
from app.repositories.queries import run_insights
from app.services.dashboard import build_dashboard
from app.services.demo_service import DemoService
from app.simulation.metrics import ambulance_waiting_hours_by_tick, hospital_recovery


# --- helpers ---------------------------------------------------------------------------
def _demo(service: DemoService, writer: PersistenceWriter, confirmations: int = 3):
    """Reset, approve the recommended proposal 15 minutes later, confirm N staff."""
    run = service.reset()
    writer.submit(build_snapshot(run, build_dashboard(service, run), "reset"))
    proposal = run.recommended_proposal()
    run, request_id = service.approve_proposal(run.run_id, proposal.id, proposal.tick + 1)
    writer.submit(build_snapshot(run, build_dashboard(service, run), "proposal_approved"))
    for _ in range(confirmations):
        run, _ = service.simulate_reply(run.run_id, request_id)
        writer.submit(build_snapshot(run, build_dashboard(service, run), "staff_confirmed"))
    return run, request_id


def _scalar(db, sql, **params):
    with db.connection() as con:
        return con.run(sql, **params)[0][0]


@pytest.fixture
def writer(database):
    w = PersistenceWriter(database, retry_initial_s=0.05, retry_max_s=0.2)
    w.start()
    yield w
    w.stop()


# --- connection and schema -------------------------------------------------------------
def test_url_parsing_redacts_password():
    s = parse_database_url("postgres://tsdbadmin:s3cret@abc.tsdb.cloud.timescale.com:3417/tsdb")
    assert (s.host, s.port, s.database, s.user, s.sslmode) == (
        "abc.tsdb.cloud.timescale.com", 3417, "tsdb", "tsdbadmin", "require")  # fmt: skip
    assert "s3cret" not in s.redacted
    with pytest.raises(ValueError):
        parse_database_url("mysql://x@y/z")


def test_ping_reports_a_real_round_trip(database):
    ping = database.ping()
    assert ping["server_version"]
    assert ping["round_trip_ms"] >= 0


def test_migrations_apply_once_and_are_idempotent(database):
    first = migrate(database)
    assert first.applied == [p.name for p in migration_files()]
    second = migrate(database)
    assert second.applied == [] and second.already_applied == first.applied
    tables = {
        r[0]
        for r in _rows(database, "SELECT table_name FROM information_schema.tables "
                                 "WHERE table_schema = current_schema()")
    }  # fmt: skip
    assert {
        "hospitals", "scenarios", "simulation_runs", "run_versions", "hospital_metrics",
        "comparison_metrics", "staffing_requests", "staff_confirmations",
        "intervention_events", "agent_activity", "schema_migrations",
    } <= tables  # fmt: skip
    if first.timescaledb_version:
        assert first.hypertable, first.hypertable_note
    else:
        assert not first.hypertable and "not available" in first.hypertable_note


def test_hypertable_statement_targets_metrics_time_column():
    sql = hypertable_statement()
    assert "create_hypertable('hospital_metrics', 'ts'" in sql
    assert "if_not_exists => TRUE" in sql


def _rows(db, sql, **params):
    with db.connection() as con:
        return con.run(sql, **params)


# --- persistence of a full demo ----------------------------------------------------------
def test_demo_flow_is_persisted(database, writer):
    service = DemoService()
    run, request_id = _demo(service, writer)
    assert writer.flush(30), writer.status
    assert writer.status.snapshots_written == 5 and not writer.status.dead_letters

    rid = run.run_id
    assert _scalar(database, "SELECT seed FROM simulation_runs WHERE run_id = :r", r=rid) == 42
    assert _scalar(database, "SELECT count(*) FROM run_versions WHERE run_id = :r", r=rid) == 5
    # Baseline stored once; Track B stored for the initial state and each recalculation.
    counts = dict(_rows(database, "SELECT track || ':' || series_version, count(*) "
                                  "FROM hospital_metrics WHERE run_id = :r GROUP BY 1",
                        r=rid))  # fmt: skip
    assert counts == {"baseline:0": 480, "triagex:0": 480, "triagex:1": 480,
                      "triagex:2": 480, "triagex:3": 480}  # fmt: skip

    req = _rows(database, "SELECT proposal_tick, approved_tick, effective_tick FROM "
                          "staffing_requests WHERE request_id = :q", q=request_id)[0]  # fmt: skip
    assert req == [17, 18, 21]
    sources = _rows(database, "SELECT source FROM staff_confirmations WHERE request_id = :q",
                    q=request_id)  # fmt: skip
    assert [s[0] for s in sources] == ["simulated"] * 3

    events = {
        k: (t, st)
        for k, t, st in _rows(
            database,
            "SELECT kind, tick, sim_time FROM intervention_events WHERE run_id = :r",
            r=rid,
        )
    }
    assert events["outreach_approved"][0] == 18
    assert events["shift_active"][0] == 21
    assert events["shift_active"][1] == tick_time(run.scenario, 21)
    assert events["shift_active"][1].astimezone(tick_time(run.scenario, 0).tzinfo).strftime(
        "%H:%M") == "17:15"  # fmt: skip

    feed = _scalar(database, "SELECT count(*) FROM agent_activity WHERE run_id = :r", r=rid)
    assert feed == len(build_dashboard(service, run).feed)


def test_stored_metrics_match_the_engine(database, writer):
    service = DemoService()
    run, _ = _demo(service, writer)
    assert writer.flush(30)
    latest = _scalar(database, "SELECT max(series_version) FROM hospital_metrics "
                               "WHERE run_id = :r AND track = 'triagex'", r=run.run_id)  # fmt: skip
    for track, result, version in (("baseline", run.baseline, 0),
                                   ("triagex", run.comparison.triagex, latest)):  # fmt: skip
        stored = {
            (h, t): (sat, occ, q, amb, staff, status)
            for h, t, sat, occ, q, amb, staff, status in _rows(
                database,
                "SELECT hospital_id, tick, saturation_pct, occupied, queue_length, "
                "ambulances_waiting, staff_on_duty, status FROM hospital_metrics "
                "WHERE run_id = :r AND track = :tr AND series_version = :v",
                r=run.run_id, tr=track, v=version,
            )
        }  # fmt: skip
        assert len(stored) == 480
        for tick in result.ticks:
            for row in tick:
                assert stored[(row.hospital_id, row.tick)] == (
                    row.saturation_pct, row.occupied, row.queue_length,
                    row.ambulance_queue_length, row.staff_on_duty, row.status.value)  # fmt: skip

    hero = dict(_rows(database, "SELECT metric, triagex FROM comparison_metrics "
                                "WHERE run_id = :r AND version = :v AND scope = 'hero'",
                      r=run.run_id, v=run.version))  # fmt: skip
    state = build_dashboard(service, run)
    assert hero["ambulance_handoff_hours"] == state.impact.primary.triagex
    assert hero["mean_wait_minutes"] == state.impact.secondary[0].triagex


def test_baseline_and_triagex_are_kept_separate(database, writer):
    service = DemoService()
    run, _ = _demo(service, writer)
    assert writer.flush(30)
    rid = run.run_id
    assert _scalar(database, "SELECT count(DISTINCT series_version) FROM hospital_metrics "
                             "WHERE run_id = :r AND track = 'baseline'", r=rid) == 1  # fmt: skip
    # Initial Track B equals the baseline row for row; the latest differs only after 17:15.
    diff_initial = _scalar(
        database,
        """
        SELECT count(*) FROM hospital_metrics b JOIN hospital_metrics t
          ON t.run_id = b.run_id AND t.hospital_id = b.hospital_id AND t.ts = b.ts
         AND t.track = 'triagex' AND t.series_version = 0
        WHERE b.run_id = :r AND b.track = 'baseline'
          AND (b.saturation_pct, b.queue_length, b.staff_on_duty)
              IS DISTINCT FROM (t.saturation_pct, t.queue_length, t.staff_on_duty)""",
        r=rid,
    )
    assert diff_initial == 0
    first_diff_tick = _scalar(
        database,
        """
        SELECT min(b.tick) FROM hospital_metrics b JOIN hospital_metrics t
          ON t.run_id = b.run_id AND t.hospital_id = b.hospital_id AND t.ts = b.ts
         AND t.track = 'triagex' AND t.series_version = 3
        WHERE b.run_id = :r AND b.track = 'baseline'
          AND (b.saturation_pct, b.queue_length, b.staff_on_duty)
              IS DISTINCT FROM (t.saturation_pct, t.queue_length, t.staff_on_duty)""",
        r=rid,
    )
    assert first_diff_tick == run.comparison.first_divergence_tick == 21
    digests = {r[0] for r in _rows(database, "SELECT baseline_digest FROM simulation_runs "
                                             "WHERE run_id = :r", r=rid)}  # fmt: skip
    assert digests == {series_digest(run.baseline)}


def test_writes_are_idempotent(database, writer):
    service = DemoService()
    run, _ = _demo(service, writer, confirmations=2)
    assert writer.flush(30)

    def table_counts():
        return {
            t: _scalar(database, f"SELECT count(*) FROM {t}")
            for t in ("simulation_runs", "run_versions", "hospital_metrics", "comparison_metrics",
                      "staffing_requests", "staff_confirmations", "intervention_events",
                      "agent_activity")
        }  # fmt: skip

    before = table_counts()
    snapshot = build_snapshot(run, build_dashboard(service, run), "staff_confirmed")
    for _ in range(3):  # replay the same snapshot, directly and through the writer
        with database.transaction() as con:
            assert write_snapshot(con, snapshot) == len(snapshot.intervention_events) + len(
                snapshot.activity
            )  # only upserted/replaced rows report as written
        writer.submit(snapshot)
    assert writer.flush(30)
    assert table_counts() == before


# --- failure behaviour -------------------------------------------------------------------
def test_database_outage_never_blocks_or_corrupts_the_demo(postgres_url, database):
    dead = Database("postgres://nobody@127.0.0.1:1/none", timeout_seconds=0.5)
    w = PersistenceWriter(dead, retry_initial_s=0.05, retry_max_s=0.1)
    w.start()
    service = DemoService()
    try:
        app = create_app(service, persistence=w)
        with TestClient(app) as client:
            started = time.perf_counter()
            state = client.post("/api/demo/reset").json()
            card = next(p for p in state["proposals"] if p["recommended"])
            state = client.post(
                f"/api/runs/{state['run']['run_id']}/proposals/{card['id']}/approve",
                json={"at_tick": card["tick"] + 1},
            ).json()
            req = state["staffing_requests"][0]["request_id"]
            r = client.post(
                f"/api/runs/{state['run']['run_id']}/staffing-requests/{req}/simulate-reply"
            )
            elapsed = (time.perf_counter() - started) * 1000
            assert r.status_code == 200 and elapsed < 1000
            reference = DemoService().reset()  # same seed, no database involved
            assert r.json()["series"][0]["baseline"][30]["saturation_pct"] == (
                reference.baseline.ticks[30][0].saturation_pct
            )

            deadline = time.monotonic() + 5
            while w.status.failed_attempts == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            status = client.get("/api/db/status").json()
            assert status["configured"] and not status["connected"]
            assert status["writer"]["pending"] == 3
            assert status["writer"]["failed_attempts"] > 0 and status["writer"]["last_error"]
            health = client.get("/api/integrations/health").json()["database"]
            assert health["status"] == "error"

        # The database comes back: queued snapshots are stored, in order.
        w.db = database
        assert w.flush(30), w.status
        assert w.status.pending == 0 and w.status.snapshots_written == 3
        run_id = r.json()["run"]["run_id"]
        assert _scalar(database, "SELECT count(*) FROM run_versions WHERE run_id = :r",
                       r=run_id) == 3  # fmt: skip
        assert _scalar(database, "SELECT count(*) FROM staff_confirmations") == 1
    finally:
        w.stop()


def test_failing_statement_is_dead_lettered_not_blocking(database, writer):
    service = DemoService()
    run = service.reset()
    good = build_snapshot(run, build_dashboard(service, run), "reset")
    bad = good.__class__(**{**{f: getattr(good, f) for f in good.__slots__},
                            "version": 99, "reason": "x" * 0, "confirmations": (
                                {"request_id": "missing", "staff_id": "s", "display_name": "d",
                                 "source": "simulated", "received_at": good.recorded_at,
                                 "recompute_ms": 1.0},)})  # fmt: skip
    writer.submit(bad)  # violates the staffing_requests foreign key
    writer.submit(good)
    assert writer.flush(30)
    assert len(writer.status.dead_letters) == 1 and "v99" in writer.status.dead_letters[0]
    assert _scalar(database, "SELECT count(*) FROM run_versions WHERE run_id = :r",
                   r=run.run_id) == 1  # fmt: skip


def test_without_database_the_api_is_unchanged():
    w = PersistenceWriter(None)
    assert not w.enabled and w.submit is not None
    with TestClient(create_app(DemoService(), persistence=w)) as client:
        assert client.post("/api/demo/reset").status_code == 200
        status = client.get("/api/db/status").json()
        assert status["configured"] is False and status["connected"] is False
        run_id = client.get("/api/demo/state").json()["run"]["run_id"]
        assert client.get(f"/api/runs/{run_id}/db/insights").status_code == 503


# --- sponsor queries over stored rows ----------------------------------------------------
def test_insight_queries_use_stored_rows_and_agree_with_the_engine(database, writer):
    service = DemoService()
    run, _ = _demo(service, writer)
    assert writer.flush(30)
    timescale = bool(writer.status.migration.timescaledb_version)
    out = run_insights(database, run.run_id, run.scenario, timescale)

    hourly = out["hourly_focus_comparison"]
    assert len(hourly["rows"]) == 12  # 6 hours x 2 tracks
    idx = hourly["columns"].index("ambulance_hours_waiting")
    focus_hours = {"baseline": 0.0, "triagex": 0.0}
    for row in hourly["rows"]:
        focus_hours[row[1]] += row[idx]
    window = run.scenario.demo_window
    grady = run.baseline.hospital_ids.index("grady")
    for track, result in (("baseline", run.baseline), ("triagex", run.comparison.triagex)):
        expected = sum(result.ticks[t][grady].ambulance_queue_length for t in range(*window)) / 4
        assert focus_hours[track] == pytest.approx(expected)

    handoff = out["ambulance_handoff_by_hospital"]
    total_b = sum(r[1] for r in handoff["rows"])
    total_t = sum(r[2] for r in handoff["rows"])
    assert total_b == pytest.approx(ambulance_waiting_hours_by_tick(run.baseline, window)[-1])
    assert total_t == pytest.approx(
        ambulance_waiting_hours_by_tick(run.comparison.triagex, window)[-1]
    )

    recovery = {r[0]: r for r in out["focus_recovery"]["rows"]}
    onset = (run.scenario.surge_onset_tick, run.scenario.horizon_ticks)
    for track, result in (("baseline", run.baseline), ("triagex", run.comparison.triagex)):
        rec = hospital_recovery(result, "grady", onset)
        stored_at = datetime.fromisoformat(recovery[track][2])
        assert stored_at == tick_time(run.scenario, rec.recovered_tick)

    history = out["confirmation_impact_history"]["rows"]
    assert [r[3] for r in history] == [0, 0, 1, 2, 3]  # confirmed staff per version
    timeline = {r[0]: r for r in out["intervention_timeline"]["rows"]}
    assert timeline["shift_active"][1] == 21 and timeline["shift_active"][6] == 16
    counts = dict(out["stored_row_counts"]["rows"])
    assert counts["hospital_metrics"] == 2400 and counts["staff_confirmations"] == 3


def test_api_exposes_status_and_insights_with_fast_confirmations(database):
    w = PersistenceWriter(database, retry_initial_s=0.05)
    service = DemoService(request_ttl=timedelta(minutes=10))
    with TestClient(create_app(service, persistence=w)) as client:
        state = client.post("/api/demo/reset").json()
        run_id = state["run"]["run_id"]
        card = next(p for p in state["proposals"] if p["recommended"])
        state = client.post(f"/api/runs/{run_id}/proposals/{card['id']}/approve",
                            json={"at_tick": card["tick"] + 1}).json()  # fmt: skip
        req = state["staffing_requests"][0]["request_id"]
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            r = client.post(f"/api/runs/{run_id}/staffing-requests/{req}/simulate-reply")
            timings.append((time.perf_counter() - started) * 1000)
            assert r.status_code == 200
        assert max(timings) < 200, timings
        assert w.flush(30)

        status = client.get("/api/db/status").json()
        assert status["connected"] and status["writer"]["snapshots_written"] == 5
        assert client.get("/api/integrations/health").json()["database"]["status"] == "ok"
        insights = client.get(f"/api/runs/{run_id}/db/insights").json()
        assert insights["source"] == "database" and insights["pending_writes_for_run"] == 0
        assert dict(insights["queries"]["stored_row_counts"]["rows"])["run_versions"] == 5
