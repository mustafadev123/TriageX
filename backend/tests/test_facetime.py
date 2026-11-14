"""Prompt 4: FaceTime card checks and verbal confirmations, through the agent API."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services.demo_service import DemoService

TOKEN = "test-agent-token-" + "x" * 40
H = {"X-TriageX-Agent-Token": TOKEN}
CALL = "d25d790a-410b-4695-beb0-28b86afbb9e4"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def agent_token(monkeypatch):
    monkeypatch.setenv("TRIAGEX_AGENT_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield TOKEN
    get_settings.cache_clear()


def _app(clock, persistence=None):
    service = DemoService(request_ttl=timedelta(minutes=10), clock=clock)
    app = create_app(service, persistence=persistence)
    app.state.facetime.clock = clock
    return app


@pytest.fixture
def client(clock, agent_token):
    with TestClient(_app(clock)) as c:
        yield c


def _approved(c) -> dict:
    state = c.post("/api/demo/reset").json()
    card = next(p for p in state["proposals"] if p["recommended"])
    r = c.post(f"/api/runs/{state['run']['run_id']}/proposals/{card['id']}/approve",
               json={"at_tick": card["tick"] + 1})  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()


def _check(c, card_id="TX-RN-1001", call=CALL, **kw):
    body = {"call_id": call, "card_id": card_id, "name": "Avery (fictional RN)",
            "role": "Registered Nurse · Emergency", "availability": "AVAILABLE",
            "confidence": 0.99} | kw  # fmt: skip
    r = c.post("/api/agent/card-check", json=body, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(c, card_id="TX-RN-1001", call=CALL, verbal="I confirm"):
    return c.post(
        "/api/agent/confirm",
        headers=H,
        json={"call_id": call, "card_id": card_id, "verbal_confirmation": verbal},
    )


def _state(c) -> dict:
    return c.get("/api/demo/state").json()


def test_requires_the_agent_token(client):
    _approved(client)
    assert client.get("/api/agent/active-request").status_code == 401
    assert client.get("/api/agent/active-request",
                      headers={"X-TriageX-Agent-Token": "wrong"}).status_code == 401  # fmt: skip
    body = {"call_id": CALL, "card_id": "TX-RN-1001", "confidence": 1}
    assert client.post("/api/agent/card-check", json=body).status_code == 401
    assert client.get("/api/integrations/health").json()["facetime"]["status"] == "starting"
    assert client.get("/api/agent/active-request", headers=H).status_code == 200
    assert client.get("/api/integrations/health").json()["facetime"]["status"] == "ok"


def test_without_a_configured_token_every_agent_call_is_refused(clock):
    get_settings.cache_clear()
    with TestClient(_app(clock)) as c:
        assert c.get("/api/agent/active-request", headers=H).status_code == 503
        assert c.get("/api/integrations/health").json()["facetime"]["status"] == "not_configured"


def test_active_request(client):
    assert client.get("/api/agent/active-request", headers=H).status_code == 404
    _approved(client)
    r = client.get("/api/agent/active-request", headers=H).json()
    assert (r["hospital"], r["requested"], r["confirmed"]) == ("Grady", 3, 0)
    assert r["shift_starts"] == "17:15"


def test_valid_card_and_verbal_confirmation_confirm_exactly_once(client):
    before = _approved(client)
    baseline_before = before["impact"]["primary"]["baseline"]
    assert client.post("/api/agent/call", headers=H,
                       json={"call_id": CALL, "event": "connected"}).status_code == 200  # fmt: skip
    check = _check(client)
    assert check["valid"] and check["reason"] == "valid"
    assert (check["staff_id"], check["display_name"]) == ("rn-avery", "Avery (fictional RN)")
    # A check alone confirms nothing.
    assert _state(client)["staffing_requests"][0]["confirmed_staff"] == 0

    r = _confirm(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed"] and not body["duplicate"]
    assert (body["confirmed_count"], body["requested"], body["shift_starts"]) == (1, 3, "17:15")
    for _ in range(3):  # retries and repeats add nothing
        again = _confirm(client)
        assert again.status_code == 200 and again.json()["duplicate"]

    state = _state(client)
    req = state["staffing_requests"][0]
    assert req["confirmed_staff"] == 1
    assert req["confirmations"][0]["source"] == "facetime"
    assert req["effective_tick"] == req["approved_tick"] + 3
    assert state["impact"]["primary"]["baseline"] == baseline_before  # the baseline never changes
    titles = [e["title"] for e in state["feed"] if e["origin"] == "facetime"]
    assert titles == ["FaceTime Connected", "Card Verified", "Staff Confirmed via FaceTime"]
    call = state["facetime"]["calls"][0]
    assert call["checks"][0]["card_id"] == "TX-RN-1001" and call["confirmed_staff"] == 1


@pytest.mark.parametrize(
    ("card_id", "fields", "reason"),
    [
        ("TX-RN-9999", {"name": "Sam (fictional RN)"}, "not_on_roster"),
        ("TX-RN-1004", {"name": "Riley (fictional RN)", "availability": "OFF DUTY"}, "off_duty"),
        ("TX-RN-1001", {"confidence": 0.5}, "low_confidence"),
        ("TX-RN-1001", {"role": "Paramedic"}, "role_mismatch"),
    ],
)
def test_invalid_cards_are_rejected_and_change_nothing(client, card_id, fields, reason):
    before = _approved(client)
    check = _check(client, card_id, **fields)
    assert not check["valid"] and check["reason"] == reason
    r = _confirm(client, card_id)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == reason
    after = _state(client)
    assert after["staffing_requests"][0]["confirmed_staff"] == 0
    assert after["impact"] == before["impact"]
    assert any(e["title"] == "Card Rejected" for e in after["feed"])


def test_unknown_card_message_names_the_card(client):
    _approved(client)
    assert _check(client, "TX-RN-9999")["message"] == (
        "Card TX-RN-9999 is not on the TriageX DEMO roster."
    )


def test_noisy_role_text_from_live_video_still_matches(client):
    _approved(client)  # live call 3 read "Nurse - Emergency"; a missing role is also allowed
    assert _check(client, role="Nurse - Emergency")["valid"]
    assert _check(client, role=None)["valid"]
    assert _check(client, card_id=" tx rn 1002 ")["card_id"] == "TX-RN-1002"


def test_confirm_needs_a_prior_check_on_the_same_call_and_card(client):
    _approved(client)
    assert _confirm(client).json()["detail"]["reason"] == "no_prior_check"
    _check(client)
    assert _confirm(client, call="another-call").json()["detail"]["reason"] == "no_prior_check"
    assert _confirm(client, card_id="TX-RN-1002").json()["detail"]["reason"] == "no_prior_check"
    assert _state(client)["staffing_requests"][0]["confirmed_staff"] == 0


def test_confirm_needs_the_words(client):
    _approved(client)
    _check(client)
    for words in ("yes", "maybe", "I don't confirm", "sure"):
        r = _confirm(client, verbal=words)
        assert r.status_code == 409 and r.json()["detail"]["reason"] == "no_verbal_confirmation"
    assert _confirm(client, verbal="Confirm.").status_code == 200


def test_check_older_than_two_minutes_is_not_enough(client, clock):
    _approved(client)
    _check(client)
    clock.now += timedelta(minutes=2, seconds=1)
    assert _confirm(client).json()["detail"]["reason"] == "no_prior_check"


def test_expired_request(client, clock):
    _approved(client)
    clock.now += timedelta(minutes=9)
    _check(client)  # valid at 9 min
    clock.now += timedelta(minutes=1, seconds=1)  # the request expires at 10 min
    assert _confirm(client).json()["detail"]["reason"] == "rejected_request"
    assert _check(client)["reason"] == "request_expired"
    assert client.get("/api/agent/active-request", headers=H).status_code == 404


def test_stale_run_after_reset(client):
    _approved(client)
    _check(client)
    client.post("/api/demo/reset")
    assert _confirm(client).json()["detail"]["reason"] == "stale_run"
    assert _state(client)["staffing_requests"] == []


def test_same_person_cannot_confirm_twice_across_calls(client):
    _approved(client)
    _check(client)
    assert _confirm(client).status_code == 200
    second = _check(client, call="second-call")
    assert second["reason"] == "already_confirmed"
    assert _state(client)["staffing_requests"][0]["confirmed_staff"] == 1


def test_three_facetime_confirmations_fill_the_request(client):
    _approved(client)
    for i, card in enumerate(("TX-RN-1001", "TX-RN-1002", "TX-RN-1003"), start=1):
        _check(client, card, call=f"call-{i}", name=None)
        assert _confirm(client, card, call=f"call-{i}").json()["confirmed_count"] == i
    req = _state(client)["staffing_requests"][0]
    assert req["status"] == "fully_confirmed"
    assert _check(client, "TX-RN-1005", call="call-4")["reason"] == "request_full"


def test_mixed_sources_keep_their_labels(client):
    # Demo order: the FaceTime confirmation first, then labeled simulated replies, which take
    # the next fictional staff members that have not confirmed.
    state = _approved(client)
    run_id, req_id = state["run"]["run_id"], state["staffing_requests"][0]["request_id"]
    _check(client)
    _confirm(client)
    for _ in range(2):
        client.post(f"/api/runs/{run_id}/staffing-requests/{req_id}/simulate-reply")
    confs = _state(client)["staffing_requests"][0]["confirmations"]
    assert [(c["staff_id"], c["source"]) for c in confs] == [
        ("rn-avery", "facetime"),
        ("rn-jordan", "simulated"),
        ("rn-morgan", "simulated"),
    ]


def test_simulated_reply_first_takes_avery_so_her_card_is_then_already_confirmed(client):
    state = _approved(client)
    run_id, req_id = state["run"]["run_id"], state["staffing_requests"][0]["request_id"]
    client.post(f"/api/runs/{run_id}/staffing-requests/{req_id}/simulate-reply")
    assert _check(client)["reason"] == "already_confirmed"
    assert _check(client, "TX-RN-1002")["valid"]


def test_call_events_and_card_checks_are_persisted(database, clock, agent_token):
    from app.repositories.persistence import PersistenceWriter

    writer = PersistenceWriter(database, retry_initial_s=0.05, retry_max_s=0.2)
    with TestClient(_app(clock, persistence=writer)) as c:
        writer.start()
        _approved(c)
        c.post("/api/agent/call", headers=H, json={"call_id": CALL, "event": "connected"})
        _check(c, "TX-RN-9999")
        _check(c)
        assert _confirm(c).status_code == 200
        c.post("/api/agent/call", headers=H, json={"call_id": CALL, "event": "ended"})
        assert writer.flush(15), writer.status.last_error
        assert writer.status.dead_letters == []

        def rows(sql):
            with database.connection() as con:
                return con.run(sql)

        assert rows("SELECT status, ended_at IS NOT NULL FROM facetime_calls") == [["ended", True]]
        checks = rows("SELECT card_id, valid, reason FROM card_checks ORDER BY checked_at, card_id")
        assert sorted(map(tuple, checks)) == [("TX-RN-1001", True, "valid"),
                                              ("TX-RN-9999", False, "not_on_roster")]  # fmt: skip
        confs = rows("SELECT staff_id, source FROM staff_confirmations")
        assert confs == [["rn-avery", "facetime"]]
        dump = json.dumps(rows("SELECT row_to_json(k)::text FROM card_checks k"))
        assert "image" not in dump and "frame" not in dump
    writer.stop()
