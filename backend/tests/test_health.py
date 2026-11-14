import json
import logging

from fastapi.testclient import TestClient

from app.config import DEMO_MESSAGE_PREFIX, Settings
from app.logging_config import JsonFormatter
from app.main import app

client = TestClient(app)


def test_health_reports_ok_and_branding():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "TriageX"
    assert "TriageX calls the people who can fix it" in body["tagline"]
    assert "not real-time hospital data" in body["notice"]


def test_demo_prefix_is_triagex():
    assert DEMO_MESSAGE_PREFIX == "TriageX DEMO"


def test_settings_defaults_do_not_require_secrets():
    settings = Settings(_env_file=None)
    assert settings.ai_primary_provider == "openai"
    assert settings.ai_fallback_provider == "gemini"
    assert settings.cors_origin_list == ["http://localhost:5173"]


def test_json_log_formatter_includes_extra_fields():
    record = logging.LogRecord("triagex", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    record.run_id = "abc"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hello x"
    assert payload["level"] == "INFO"
    assert payload["run_id"] == "abc"
