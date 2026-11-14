"""Prompt 6: grounded operational brief, Gemini primary -> Gemini fallback -> template."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.brief import (
    BRIEF_SCHEMA,
    BriefService,
    GeminiProvider,
    ProviderError,
    build_facts,
    render,
)
from app.services.dashboard import build_dashboard
from app.services.demo_service import DemoService

GOOD = {
    "headline": "At {clock}, {focus_hospital} is at {focus_saturation_pct} occupancy.",
    "bullets": [
        "The two-hour forecast is {focus_forecast_pct}.",
        "{focus_ambulances_waiting} ambulances are waiting to offload.",
    ],
    "action": "Keep monitoring {focus_hospital}.",
}


class FakeProvider:
    name = "gemini"

    def __init__(self, model, *outputs):
        self.model = model
        self.outputs = list(outputs)
        self.calls = 0

    def generate(self, system, facts, schema):
        self.calls += 1
        out = self.outputs.pop(0) if self.outputs else ProviderError("server", "exhausted")
        if isinstance(out, Exception):
            raise out
        return out


def demo_state(confirm=0, approve=True):
    demo = DemoService()
    run = demo.create_run()
    if approve:
        card = run.recommended_proposal()
        run, req = demo.approve_proposal(run.run_id, card.id, 18)
        for _ in range(confirm):
            demo.simulate_reply(run.run_id, req)
    return build_dashboard(demo, run)


def test_primary_success_renders_numbers_from_the_api():
    state = demo_state()
    primary = FakeProvider("gemini-3.1-flash-lite", GOOD)
    fallback = FakeProvider("gemini-3.5-flash-lite")
    brief = BriefService(primary, fallback).generate(state, 18)
    assert (brief.provider, brief.model, brief.fallback_reason) == (
        "gemini",
        "gemini-3.1-flash-lite",
        None,
    )
    _, facts = build_facts(state, 18)
    assert brief.headline == (
        f"At 16:30, Grady is at {facts['focus_saturation_pct'].value} occupancy."
    )
    assert brief.metric_keys == [
        "clock",
        "focus_hospital",
        "focus_saturation_pct",
        "focus_forecast_pct",
        "focus_ambulances_waiting",
    ]
    assert fallback.calls == 0


@pytest.mark.parametrize("kind", ["timeout", "rate_limited", "server", "connection", "auth"])
def test_primary_failure_falls_back_to_gemini_fallback(kind):
    state = demo_state()
    primary = FakeProvider("p", ProviderError(kind))
    service = BriefService(primary, FakeProvider("f", GOOD))
    brief = service.generate(state, 18)
    assert brief.provider == "gemini_fallback" and brief.model == "f"
    assert brief.fallback_reason == f"primary p: {kind}"
    status, detail = service.health("primary")
    if kind == "auth":
        assert status == "error" and "misconfigured" in detail
    else:
        assert status == "starting"


def test_invalid_output_is_retried_once_then_falls_back():
    state = demo_state()
    numeral = dict(GOOD, headline="Grady is at 95% occupancy.")  # a number the model wrote itself
    primary = FakeProvider("p", numeral, numeral)
    brief = BriefService(primary, FakeProvider("f", GOOD)).generate(state, 18)
    assert primary.calls == 2  # one retry
    assert brief.provider == "gemini_fallback"
    assert brief.fallback_reason == "primary p: invalid"
    retried = FakeProvider("p", numeral, GOOD)
    assert BriefService(retried, None).generate(state, 18).provider == "gemini"


def test_both_fail_gives_the_labeled_template():
    state = demo_state(confirm=1)
    brief = BriefService(
        FakeProvider("p", ProviderError("timeout")),
        FakeProvider("f", ProviderError("rate_limited")),
    ).generate(state, 18)
    assert brief.provider == "template" and brief.model is None
    assert brief.fallback_reason == "primary p: timeout; fallback f: rate_limited"
    assert "1 of 3 staff confirmed" in " ".join(brief.bullets)
    unconfigured = BriefService(None, None).generate(state, 18)
    assert unconfigured.provider == "template"
    assert unconfigured.fallback_reason == "primary: not configured; fallback: not configured"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (dict(GOOD, action="Call in 3 staff."), "a number was written without a placeholder"),
        (dict(GOOD, action="Call in three staff."), "a number was written without a placeholder"),
        (dict(GOOD, headline="The I-eighty-five pileup."), "a number was written without a"),
        (dict(GOOD, action="Watch {made_up_metric}."), "unknown metric_key 'made_up_metric'"),
        (dict(GOOD, bullets=[]), "bullets must be 1 to 4 non-empty strings"),
        ({"headline": "x"}, "missing headline, bullets or action"),
        (dict(GOOD, action="Use {focus_hospital"), "malformed placeholder"),
    ],
)
def test_validator_rejects_ungrounded_output(raw, reason):
    _, facts = build_facts(demo_state(), 18)
    with pytest.raises(ValueError, match=reason):
        render(raw, facts)


def test_spoiler_rule_no_result_before_the_end_of_the_window():
    state = demo_state(confirm=3)
    _, before = build_facts(state, 18)  # 16:30: futures not yet diverged
    assert not any(k.startswith(("result_", "amb_hours")) for k in before)
    _, mid = build_facts(state, 24)  # after the 17:15 divergence: only "so far" values
    assert "amb_hours_saved_so_far" in mid and not any(k.startswith("result_") for k in mid)
    tick, end = build_facts(state, 999)  # clamped to the last tick of the demo window
    assert tick == state.curves.window.end_tick - 1
    assert end["result_saved"].value == f"{abs(state.impact.primary.delta):.2f} h"
    assert end["result_saved"].value == "9.25 h"  # the verified I-85 seed-42 result
    _, early = build_facts(demo_state(approve=False), 14)  # before the proposal
    assert "proposal_staff" not in early and "staff_requested" not in early


def test_gemini_provider_request_and_error_mapping():
    seen = []

    def ok(request):
        seen.append(request)
        text = json.dumps(GOOD)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})

    p = GeminiProvider("key-123", "gemini-3.1-flash-lite", transport=httpx.MockTransport(ok))
    assert p.generate("sys", {"clock": {"value": "16:30"}}, BRIEF_SCHEMA) == GOOD
    req = seen[0]
    assert req.url.path.endswith("/gemini-3.1-flash-lite:generateContent")
    assert req.headers["x-goog-api-key"] == "key-123"
    body = json.loads(req.content)
    assert body["generationConfig"]["responseJsonSchema"] == BRIEF_SCHEMA
    assert "temperature" not in body["generationConfig"]

    cases = {
        429: "rate_limited",
        500: "server",
        503: "server",
        403: "auth",
        404: "config",
    }
    for status, kind in cases.items():
        bad = GeminiProvider(
            "k",
            "m",
            transport=httpx.MockTransport(
                lambda r, s=status: httpx.Response(s, json={"error": {"message": "x"}})
            ),
        )
        with pytest.raises(ProviderError) as exc:
            bad.generate("s", {}, BRIEF_SCHEMA)
        assert exc.value.kind == kind
    key = GeminiProvider(
        "k",
        "m",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                400, json={"error": {"message": "API key not valid. Please pass a valid API key."}}
            )
        ),
    )
    with pytest.raises(ProviderError, match="auth"):
        key.generate("s", {}, BRIEF_SCHEMA)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError, match="timeout"):
        GeminiProvider("k", "m", transport=httpx.MockTransport(timeout)).generate("s", {}, {})
    junk = GeminiProvider(
        "k",
        "m",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
            )
        ),
    )
    with pytest.raises(ProviderError, match="invalid"):
        junk.generate("s", {}, {})


def test_brief_api_and_health():
    service = BriefService(FakeProvider("p", GOOD), FakeProvider("f"))
    with TestClient(create_app(briefs=service)) as c:
        health = c.get("/api/integrations/health").json()
        assert health["ai_primary"]["status"] == "starting"
        state = c.post("/api/demo/reset").json()
        run_id = state["run"]["run_id"]
        assert c.get(f"/api/runs/{run_id}/brief").status_code == 404
        r = c.post(f"/api/runs/{run_id}/brief", json={"at_tick": 18})
        assert r.status_code == 200, r.text
        brief = r.json()
        assert brief["provider"] == "gemini" and brief["clock"] == "16:30"
        assert brief["notice"].startswith("Synthetic")
        assert c.get(f"/api/runs/{run_id}/brief").json()["brief_id"] == brief["brief_id"]
        assert c.post("/api/runs/nope/brief", json={"at_tick": 18}).status_code == 404
        health = c.get("/api/integrations/health").json()
        assert health["ai_primary"]["status"] == "ok"
        assert health["ai_fallback"]["status"] == "starting"


def test_without_a_key_the_brief_is_the_template_and_health_says_so():
    with TestClient(create_app()) as c:
        health = c.get("/api/integrations/health").json()
        assert health["ai_primary"]["status"] == "not_configured"
        state = c.post("/api/demo/reset").json()
        brief = c.post(f"/api/runs/{state['run']['run_id']}/brief", json={"at_tick": 14}).json()
        assert brief["provider"] == "template"


def test_briefs_are_persisted(database):
    from app.repositories.persistence import PersistenceWriter

    writer = PersistenceWriter(database, retry_initial_s=0.05, retry_max_s=0.2)
    service = BriefService(
        FakeProvider("p", ProviderError("rate_limited")), FakeProvider("f", GOOD)
    )
    with TestClient(create_app(briefs=service, persistence=writer)) as c:
        writer.start()
        state = c.post("/api/demo/reset").json()
        c.post(f"/api/runs/{state['run']['run_id']}/brief", json={"at_tick": 18})
        assert writer.flush(15), writer.status.last_error
        with database.connection() as con:
            rows = con.run("SELECT provider, model, fallback_reason, metric_keys FROM ai_briefs")
        assert rows == [
            [
                "gemini_fallback",
                "f",
                "primary p: rate_limited",
                [
                    "clock",
                    "focus_hospital",
                    "focus_saturation_pct",
                    "focus_forecast_pct",
                    "focus_ambulances_waiting",
                ],
            ]
        ]
    writer.stop()
