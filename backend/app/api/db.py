"""Database status and stored-data insight endpoints (Tiger Data)."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from app.repositories.db import CONNECTION_ERRORS
from app.repositories.persistence import PersistenceWriter
from app.repositories.queries import run_insights
from app.services.demo_service import NotFoundError

router = APIRouter(tags=["database"])


def _writer(request: Request) -> PersistenceWriter:
    return request.app.state.persistence


PING_CACHE_SECONDS = 3.0
_ping_cache: dict[int, tuple[float, dict | None, str | None]] = {}
_ping_lock = threading.Lock()


def _cached_ping(writer: PersistenceWriter) -> tuple[dict | None, str | None]:
    """A real round trip at most every few seconds, so health polling stays fast even
    while the database is unreachable (each live check is bounded to 2 seconds)."""
    key = id(writer.db)
    with _ping_lock:
        hit = _ping_cache.get(key)
        if hit and time.monotonic() - hit[0] < PING_CACHE_SECONDS:
            return hit[1], hit[2]
    try:
        result, error = writer.db.ping(timeout=2.0), None
    except Exception as exc:  # report every failure, never a false success
        result, error = None, f"{type(exc).__name__}: {exc}"[:300]
    with _ping_lock:
        _ping_cache[key] = (time.monotonic(), result, error)
    return result, error


def database_status(writer: PersistenceWriter) -> dict:
    """Live connectivity plus writer state. Never reports success without a real query."""
    status = writer.status
    body: dict = {
        "configured": writer.enabled,
        "connected": False,
        "server_version": None,
        "timescaledb_version": None,
        "hypertable": False,
        "round_trip_ms": None,
        "error": None,
        "writer": {
            "pending": status.pending,
            "snapshots_written": status.snapshots_written,
            "rows_written": status.rows_written,
            "failed_attempts": status.failed_attempts,
            "dropped": status.dropped,
            "dead_letters": list(status.dead_letters),
            "last_error": status.last_error,
            "last_error_at": status.last_error_at,
            "last_success_at": status.last_success_at,
            "last_write_ms": status.last_write_ms,
            "migrated": status.migrated,
            "migration": asdict(status.migration) if status.migration else None,
        },
    }
    if not writer.enabled:
        return body
    body["target"] = writer.db.redacted_url
    ping, error = _cached_ping(writer)
    if ping is None:
        body["error"] = error
    else:
        body.update(connected=True, **ping)
        body["hypertable"] = bool(status.migration and status.migration.hypertable)
    return body


@router.get("/db/status")
def db_status(request: Request) -> dict:
    return database_status(_writer(request))


@router.get("/runs/{run_id}/db/insights")
def db_insights(request: Request, run_id: str) -> dict:
    """Queries over this run's persisted rows (not the in-memory state)."""
    writer = _writer(request)
    if not writer.enabled:
        raise HTTPException(503, "No database configured (set DATABASE_URL)")
    try:
        run = request.app.state.demo_service.get_run(run_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    timescale = bool(writer.status.migration and writer.status.migration.timescaledb_version)
    try:
        results = run_insights(writer.db, run_id, run.scenario, timescale)
    except CONNECTION_ERRORS as exc:
        raise HTTPException(503, f"Database unavailable: {exc}") from exc
    return {
        "run_id": run_id,
        "source": "database",
        "time_bucket_function": "time_bucket" if timescale else "date_bin",
        "pending_writes_for_run": writer.pending_for(run_id),
        "queries": results,
    }
