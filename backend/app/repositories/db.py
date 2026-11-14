"""PostgreSQL access for Tiger Data (TimescaleDB) using the pure-Python pg8000 driver.

pg8000 is used instead of psycopg because Windows Application Control on the demo laptop
blocks psycopg's native libpq DLL. pg8000 needs no native code.

`Database` wraps a small thread-safe connection pool. Credentials come only from
DATABASE_URL (never logged); the URL's password is redacted in every message.
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import pg8000.native
from pg8000.exceptions import DatabaseError, InterfaceError

log = logging.getLogger("triagex.db")

# Errors that mean "the database is unreachable", as opposed to a bad statement.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (InterfaceError, OSError, TimeoutError)


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    host: str
    port: int
    database: str
    user: str
    password: str | None
    sslmode: str
    schema: str | None

    @property
    def redacted(self) -> str:
        return f"postgres://{self.user}:***@{self.host}:{self.port}/{self.database}"


def parse_database_url(url: str, schema: str | None = None) -> ConnectionSettings:
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError("DATABASE_URL must start with postgres:// or postgresql://")
    if not parsed.hostname:
        raise ValueError("DATABASE_URL has no host")
    query = parse_qs(parsed.query)
    return ConnectionSettings(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=unquote(parsed.path.lstrip("/")) or "postgres",
        user=unquote(parsed.username or "postgres"),
        password=unquote(parsed.password) if parsed.password else None,
        sslmode=query.get(
            "sslmode", ["prefer" if parsed.hostname in ("127.0.0.1", "localhost") else "require"]
        )[0],  # fmt: skip
        schema=schema,
    )


def _ssl_context(sslmode: str) -> ssl.SSLContext | None:
    """libpq-compatible sslmode handling."""
    if sslmode in ("disable", "allow", "prefer"):
        return None
    if sslmode in ("verify-ca", "verify-full"):
        context = ssl.create_default_context()
        context.check_hostname = sslmode == "verify-full"
        return context
    if sslmode == "require":  # encrypted, certificate not verified (libpq semantics)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    raise ValueError(f"Unsupported sslmode '{sslmode}'")


class Database:
    def __init__(
        self,
        url: str,
        *,
        schema: str | None = None,
        pool_size: int = 3,
        timeout_seconds: float = 10.0,
        application_name: str = "triagex",
    ):
        self.settings = parse_database_url(url, schema)
        self.pool_size = pool_size
        self.timeout_seconds = timeout_seconds
        self.application_name = application_name
        self._idle: list[pg8000.native.Connection] = []
        self._open = 0
        self._cond = threading.Condition()
        self._closed = False

    @property
    def redacted_url(self) -> str:
        return self.settings.redacted

    def _connect(self, timeout: float | None = None) -> pg8000.native.Connection:
        s = self.settings
        con = pg8000.native.Connection(
            user=s.user,
            password=s.password,
            host=s.host,
            port=s.port,
            database=s.database,
            ssl_context=_ssl_context(s.sslmode),
            timeout=timeout or self.timeout_seconds,
            application_name=self.application_name,
            tcp_keepalive=True,
        )
        if s.schema:
            con.run(f"CREATE SCHEMA IF NOT EXISTS {pg8000.native.identifier(s.schema)}")
            con.run(f"SET search_path TO {pg8000.native.identifier(s.schema)}")
        return con

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[pg8000.native.Connection]:
        """Borrow a pooled connection. Broken connections are discarded, not reused.

        `timeout` (seconds) bounds both waiting for a free connection and opening a new one.
        """
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        with self._cond:
            while True:
                if self._closed:
                    raise InterfaceError("Database pool is closed")
                if self._idle:
                    con = self._idle.pop()
                    break
                if self._open < self.pool_size:
                    self._open += 1
                    con = None
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    raise TimeoutError("Timed out waiting for a database connection")
        if con is None:
            try:
                con = self._connect(timeout)
            except BaseException:
                with self._cond:
                    self._open -= 1
                    self._cond.notify()
                raise
        healthy = True
        try:
            yield con
        except CONNECTION_ERRORS:
            healthy = False
            raise
        except DatabaseError:
            # A failed statement may leave a transaction open; reset before reuse.
            try:
                con.run("ROLLBACK")
            except Exception:
                healthy = False
            raise
        finally:
            with self._cond:
                if healthy and not self._closed:
                    self._idle.append(con)
                else:
                    self._open -= 1
                    try:
                        con.close()
                    except Exception:
                        pass
                self._cond.notify()

    @contextmanager
    def transaction(self) -> Iterator[pg8000.native.Connection]:
        with self.connection() as con:
            con.run("START TRANSACTION")
            try:
                yield con
            except BaseException:
                try:
                    con.run("ROLLBACK")
                except Exception:
                    pass
                raise
            con.run("COMMIT")

    def ping(self, timeout: float = 2.0) -> dict:
        """Run a real round trip (bounded by `timeout`). Raises on failure."""
        started = time.perf_counter()
        with self.connection(timeout=timeout) as con:
            version = con.run("SHOW server_version")[0][0]
            ts = con.run("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        return {
            "server_version": version,
            "timescaledb_version": ts[0][0] if ts else None,
            "round_trip_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    def close(self) -> None:
        with self._cond:
            self._closed = True
            while self._idle:
                con = self._idle.pop()
                self._open -= 1
                try:
                    con.close()
                except Exception:
                    pass
            self._cond.notify_all()
