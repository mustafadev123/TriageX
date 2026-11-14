"""Test configuration.

Tests never use a developer's DATABASE_URL from .env: it is blanked here before the app
is imported. Database tests use TRIAGEX_TEST_DATABASE_URL, or start a throwaway local
PostgreSQL with pgserver; if neither is available they are skipped (and reported).
"""

import os

os.environ["DATABASE_URL"] = ""
# Never use real Chert credentials or phone numbers in tests (Phase 6).
for _key in ("CHERT_SIGNING_SECRET", "CHERT_TENANT", "CHERT_WEBHOOK_SECRET",
             "DEMO_ALLOWED_PHONES", "PUBLIC_BASE_URL", "CHERT_API_BASE",
             "TRIAGEX_AGENT_TOKEN", "CHERT_API_KEY", "CHERT_PROJECT_ID", "CHERT_LINE_ID",
             "CHERT_SHARED_API_BASE", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL",
             "GEMINI_FALLBACK_MODEL"):  # fmt: skip
    os.environ[_key] = ""

import logging  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402

_PG_SERVER = None


@pytest.fixture(scope="session")
def postgres_url():
    """A real PostgreSQL server for database tests."""
    url = os.environ.get("TRIAGEX_TEST_DATABASE_URL")
    if url:
        return url
    global _PG_SERVER
    try:
        import pgserver
    except ImportError:
        pytest.skip("No TRIAGEX_TEST_DATABASE_URL and pgserver is not installed")
    logging.getLogger("pgserver").setLevel(logging.WARNING)  # quiet shutdown logging
    try:
        _PG_SERVER = pgserver.get_server(
            tempfile.mkdtemp(prefix="triagex-pg-"), cleanup_mode="stop"
        )
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.skip(f"Could not start a local PostgreSQL server: {exc}")
    return _PG_SERVER.get_uri()


@pytest.fixture
def database(postgres_url):
    """A Database bound to a fresh schema, dropped after the test."""
    from app.repositories.db import Database

    schema = f"t_{uuid.uuid4().hex[:10]}"
    db = Database(postgres_url, schema=schema, pool_size=2, timeout_seconds=10)
    yield db
    try:
        with db.connection() as con:
            con.run(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        db.close()
