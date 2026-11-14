"""Applies db/migrations/*.sql in order, then converts hospital_metrics to a hypertable
when TimescaleDB is available. Safe to run repeatedly.

    uv run python -m app.repositories.migrations
"""

from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.config import REPO_ROOT
from app.repositories.db import Database

log = logging.getLogger("triagex.migrations")

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
HYPERTABLE = "hospital_metrics"
HYPERTABLE_TIME_COLUMN = "ts"
HYPERTABLE_CHUNK_INTERVAL = "1 day"


@dataclass(slots=True)
class MigrationReport:
    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    timescaledb_version: str | None = None
    hypertable: bool = False
    hypertable_note: str = ""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))


def hypertable_statement(table: str = HYPERTABLE) -> str:
    """create_hypertable works on every TimescaleDB 2.x, including Tiger Cloud."""
    return (
        f"SELECT create_hypertable('{table}', '{HYPERTABLE_TIME_COLUMN}', "
        f"chunk_time_interval => INTERVAL '{HYPERTABLE_CHUNK_INTERVAL}', "
        f"if_not_exists => TRUE, migrate_data => TRUE)"
    )


def _timescaledb_version(con) -> str | None:
    rows = con.run("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    if rows:
        return rows[0][0]
    available = con.run("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    if not available:
        return None
    try:
        con.run("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except Exception as exc:  # e.g. insufficient privilege
        log.warning("timescaledb is available but could not be enabled: %s", exc)
        con.run("ROLLBACK")
        return None
    rows = con.run("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    return rows[0][0] if rows else None


def is_hypertable(con, table: str = HYPERTABLE) -> bool:
    try:
        rows = con.run(
            "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :t",
            t=table,
        )
    except Exception:
        con.run("ROLLBACK")
        return False
    return bool(rows)


def migrate(db: Database, directory: Path = MIGRATIONS_DIR) -> MigrationReport:
    report = MigrationReport()
    with db.connection() as con:
        con.run(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, checksum text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {
            row[0]: row[1] for row in con.run("SELECT version, checksum FROM schema_migrations")
        }

    for path in migration_files(directory):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise RuntimeError(
                    f"Migration {path.name} changed after it was applied; add a new migration"
                )
            report.already_applied.append(path.name)
            continue
        with db.transaction() as con:
            con.run(sql)
            con.run(
                "INSERT INTO schema_migrations (version, checksum) VALUES (:v, :c)",
                v=path.name,
                c=checksum,
            )
        report.applied.append(path.name)
        log.info("migration applied", extra={"migration": path.name})

    with db.connection() as con:
        report.timescaledb_version = _timescaledb_version(con)
        if report.timescaledb_version is None:
            report.hypertable_note = "TimescaleDB not available; hospital_metrics is a plain table"
        else:
            if not is_hypertable(con):
                con.run(hypertable_statement())
            report.hypertable = is_hypertable(con)
            report.hypertable_note = (
                f"hospital_metrics is a hypertable (TimescaleDB {report.timescaledb_version})"
                if report.hypertable
                else "TimescaleDB present but hypertable conversion did not succeed"
            )
    return report


def main() -> int:
    from app.config import get_settings
    from app.logging_config import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.database_url:
        print("DATABASE_URL is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 2
    db = Database(settings.database_url, schema=settings.database_schema)
    try:
        print(f"Connecting to {db.redacted_url}")
        report = migrate(db)
        print(f"Applied: {report.applied or 'none'}")
        print(f"Already applied: {report.already_applied or 'none'}")
        print(report.hypertable_note)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
