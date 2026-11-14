"""End-to-end database verification for TriageX.

Connects to DATABASE_URL (from the environment or the repo's .env), applies migrations,
runs the I-85 demo flow (reset -> approve at 16:30 -> 3 simulated confirmations), writes
it through the same persistence path the API uses, then reads the rows back with the
sponsor-demo queries and checks them against the in-memory engine.

    cd backend
    uv run python ../scripts/verify_db.py            # uses DATABASE_URL
    uv run python ../scripts/verify_db.py --keep     # keep the verification run's rows

Exit code 0 only if every check passed against a real database.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.repositories.db import Database  # noqa: E402
from app.repositories.migrations import migrate  # noqa: E402
from app.repositories.persistence import PersistenceWriter, build_snapshot  # noqa: E402
from app.repositories.queries import run_insights  # noqa: E402
from app.services.dashboard import build_dashboard  # noqa: E402
from app.services.demo_service import DemoService  # noqa: E402
from app.simulation.metrics import ambulance_waiting_hours_by_tick  # noqa: E402


def _table(result: dict, limit: int = 14) -> str:
    cols = result["columns"]
    rows = [[("" if v is None else str(v))[:32] for v in r] for r in result["rows"][:limit]]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]  # fmt: skip
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths, strict=True))  # noqa: E731
    return "\n".join([line(cols), line(["-" * w for w in widths]), *(line(r) for r in rows)])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the verification rows")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.database_url:
        print("FAIL: DATABASE_URL is not set (add it to .env; see .env.example).")
        return 2
    db = Database(settings.database_url, schema=settings.database_schema, timeout_seconds=15)
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    try:
        print(f"Target: {db.redacted_url}")
        ping = db.ping(timeout=15)
        print(f"Connected: PostgreSQL {ping['server_version']}, TimescaleDB "
              f"{ping['timescaledb_version'] or 'not installed'}, {ping['round_trip_ms']} ms")  # fmt: skip
        report = migrate(db)
        print(f"Migrations applied: {report.applied or 'none new'}; {report.hypertable_note}")

        writer = PersistenceWriter(db, auto_migrate=False)
        writer.status.migrated = True
        writer.start()
        service = DemoService()
        run = service.reset()
        writer.submit(build_snapshot(run, build_dashboard(service, run), "reset"))
        proposal = run.recommended_proposal()
        run, request_id = service.approve_proposal(run.run_id, proposal.id, proposal.tick + 1)
        writer.submit(build_snapshot(run, build_dashboard(service, run), "proposal_approved"))
        for _ in range(3):
            run, _ = service.simulate_reply(run.run_id, request_id)
            writer.submit(build_snapshot(run, build_dashboard(service, run), "staff_confirmed"))
        started = time.perf_counter()
        flushed = writer.flush(120)
        writer.stop()
        print(f"Wrote {writer.status.snapshots_written} snapshots / {writer.status.rows_written} "
              f"rows in {time.perf_counter() - started:.2f} s (run {run.run_id})")  # fmt: skip

        print("\nChecks:")
        check("all snapshots written", flushed and writer.status.snapshots_written == 5)
        check("no dead letters", not writer.status.dead_letters)
        out = run_insights(db, run.run_id, run.scenario, bool(report.timescaledb_version))
        counts = dict(out["stored_row_counts"]["rows"])
        check("hospital_metrics rows = 5 hospitals x 96 ticks x 5 series",
              counts.get("hospital_metrics") == 2400)
        check("3 staff confirmations stored", counts.get("staff_confirmations") == 3)
        window = run.scenario.demo_window
        handoff = out["ambulance_handoff_by_hospital"]["rows"]
        check(
            "stored ambulance hours match the engine (both tracks)",
            abs(sum(r[1] for r in handoff)
                - ambulance_waiting_hours_by_tick(run.baseline, window)[-1]) < 1e-6
            and abs(sum(r[2] for r in handoff)
                    - ambulance_waiting_hours_by_tick(run.comparison.triagex, window)[-1]) < 1e-6,
        )  # fmt: skip
        events = {r[0]: r for r in out["intervention_timeline"]["rows"]}
        check("approval stored at tick 18 (16:30), shift active at tick 21 (17:15)",
              events.get("outreach_approved", [None, None])[1] == 18
              and events.get("shift_active", [None, None])[1] == 21)  # fmt: skip
        if report.timescaledb_version:
            check("hospital_metrics is a TimescaleDB hypertable", report.hypertable)

        for name in ("stored_row_counts", "hourly_focus_comparison", "ambulance_handoff_by_hospital",
                     "intervention_timeline", "focus_recovery", "confirmation_impact_history"):  # fmt: skip
            print(f"\n== {name} ({out[name]['rows'].__len__()} rows)\n{_table(out[name])}")

        if not args.keep:
            with db.transaction() as con:
                con.run("DELETE FROM simulation_runs WHERE run_id = :r", r=run.run_id)
            print(f"\nRemoved verification run {run.run_id} (use --keep to retain it).")
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        db.close()

    ok = all(passed for _, passed in checks)
    print("\nRESULT:", "VERIFIED" if ok else "FAILED", f"({sum(p for _, p in checks)}/{len(checks)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
