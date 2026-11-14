"""Phase 3 API: single-screen dashboard state, demo flow, reset, errors, and latency."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.demo_service import DemoService
from app.simulation.comparison import run_comparison
from app.simulation.interventions import StaffingIntervention
from app.simulation.metrics import ambulance_waiting_hours_by_tick
from app.simulation.scenarios import SCENARIOS


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def client(clock):
    service = DemoService(request_ttl=timedelta(minutes=10), clock=clock)
    with TestClient(create_app(service)) as c:
        yield c


def _reset(client, **body):
    r = client.post("/api/demo/reset", json=body or None)
    assert r.status_code == 200, r.text
    return r.json()


def _approve_recommended(client, state):
    pid = next(p["id"] for p in state["proposals"] if p["recommended"])
    r = client.post(f"/api/runs/{state['run']['run_id']}/proposals/{pid}/approve")
    assert r.status_code == 200, r.text
    return r.json()


def _reply(client, state, **body):
    run_id = state["run"]["run_id"]
    request_id = state["staffing_requests"][0]["request_id"]
    return client.post(
        f"/api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply", json=body or None
    )


def _confirm_all(client):
    state = _approve_recommended(client, _reset(client))
    for _ in range(state["staffing_requests"][0]["requested_staff"]):
        r = _reply(client, state)
        assert r.status_code == 200, r.text
        state = r.json()
    return state


# --- initial state --------------------------------------------------------------------
def test_reset_returns_ready_to_render_i85_state(client):
    s = _reset(client)
    assert s["run"]["scenario"]["id"] == "i85_pileup"
    assert s["run"]["seed"] == 42
    assert "not real-time hospital data" in s["run"]["notice"]
    assert len(s["timeline"]["clocks"]) == 96
    window = s["timeline"]["demo_window"]
    assert (window["start_clock"], window["end_clock"]) == ("15:00", "21:00")
    assert len(s["curves"]["points"]) == 24
    assert s["curves"]["focus_hospital_id"] == "grady"
    assert len(s["hospitals"]) == 5 and len(s["series"]) == 5
    assert all(len(h["baseline"]) == len(h["triagex"]) == 96 for h in s["series"])
    assert s["divergence"] is None
    assert s["impact"]["state"] == "awaiting_intervention"
    assert s["impact"]["primary"]["delta"] == 0
    assert s["impact"]["primary"]["headline"] == "No difference yet"
    assert sum(p["recommended"] for p in s["proposals"]) == 1
    rec = next(p for p in s["proposals"] if p["recommended"])
    assert (rec["hospital_id"], rec["kind"], rec["recommended_staff"]) == ("grady", "staffing", 3)
    assert "model_limitations" not in s  # diagnostics live on their own endpoint


def test_tracks_are_identical_before_any_action(client):
    s = _reset(client)
    for h in s["series"]:
        assert h["baseline"] == h["triagex"]
    for p in s["curves"]["points"]:
        assert p["ambulance_hours_saved_so_far"] == 0
        assert not p["diverged"]


def test_reset_is_reproducible(client):
    def stable(state):
        return {k: state[k] for k in ("series", "curves", "impact", "transitions", "flows",
                                      "markers", "bands", "proposals", "timeline")}  # fmt: skip

    first = _reset(client)
    _confirm_all(client)
    second = _reset(client)
    assert first["run"]["run_id"] != second["run"]["run_id"]
    assert stable(first) == stable(second)
    assert [(e["tick"], e["title"], e["detail"]) for e in first["feed"]] == [
        (e["tick"], e["title"], e["detail"]) for e in second["feed"]
    ]


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_scenario_can_be_reset_and_has_a_recommended_proposal(client, scenario_id):
    s = _reset(client, scenario_id=scenario_id)
    assert s["run"]["scenario"]["id"] == scenario_id
    assert len(s["curves"]["points"]) == 24
    assert any(p["recommended"] for p in s["proposals"])


# --- demo flow ------------------------------------------------------------------------
def test_approval_alone_changes_no_outcome(client):
    s = _approve_recommended(client, _reset(client))
    req = s["staffing_requests"][0]
    assert req["status"] == "awaiting_replies" and req["confirmed_staff"] == 0
    assert req["effective_tick"] - req["approved_tick"] == 3  # 45-minute lead time
    assert s["divergence"] is None
    assert s["impact"]["state"] == "awaiting_intervention"
    assert any(e["title"] == "Outreach Approved" and e["origin"] == "operator" for e in s["feed"])
    assert {b["kind"] for b in s["bands"]} >= {"demo_window", "staff_lead_time"}


def test_confirmations_move_only_the_future_after_activation(client):
    initial = _reset(client)
    s = _confirm_all(client)
    req = s["staffing_requests"][0]
    assert req["status"] == "fully_confirmed" and req["confirmed_staff"] == 3
    assert s["divergence"]["tick"] >= req["effective_tick"]

    div = s["divergence"]["tick"]
    for p in s["curves"]["points"]:
        if p["tick"] < div:
            assert p["baseline_focus_saturation_pct"] == p["triagex_focus_saturation_pct"]
            assert p["ambulance_hours_saved_so_far"] == 0
            assert not p["diverged"]
        else:
            assert p["diverged"]
    phases = {p["tick"]: p["phase"] for p in s["curves"]["points"]}
    assert phases[req["approved_tick"]] == "staff_en_route"
    assert phases[req["effective_tick"]] == "shift_active"
    # Baseline history is never rewritten.
    for before, after in zip(initial["series"], s["series"], strict=True):
        assert before["baseline"] == after["baseline"]


def test_hero_metrics_come_from_the_engine(client):
    s = _confirm_all(client)
    req = s["staffing_requests"][0]
    c = run_comparison(
        "i85_pileup",
        42,
        (StaffingIntervention(req["request_id"], "grady", req["approved_tick"], 3),),
    )
    window = c.scenario.demo_window
    primary = s["impact"]["primary"]
    assert primary["baseline"] == ambulance_waiting_hours_by_tick(c.baseline, window)[-1]
    assert primary["triagex"] == ambulance_waiting_hours_by_tick(c.triagex, window)[-1]
    assert primary["delta"] == round(primary["triagex"] - primary["baseline"], 2)
    assert primary["pct_change"] == round(primary["delta"] / primary["baseline"] * 100, 1)
    wait = next(m for m in s["impact"]["secondary"] if m["key"] == "mean_wait_minutes")
    assert wait["baseline"] == c.window_baseline_metrics.mean_wait_minutes
    assert wait["triagex"] == c.window_triagex_metrics.mean_wait_minutes
    last = s["curves"]["points"][-1]
    assert last["baseline_ambulance_hours_cumulative"] == primary["baseline"]
    assert last["ambulance_hours_saved_so_far"] == -primary["delta"]


def test_simulated_replies_are_labeled(client):
    s = _confirm_all(client)
    assert all(c["source"] == "simulated" for c in s["staffing_requests"][0]["confirmations"])
    confirmed = [e for e in s["feed"] if e["title"] == "Staff Confirmed"]
    assert len(confirmed) == 3
    assert all(e["origin"] == "simulated_reply" for e in confirmed)
    titles = [e["title"] for e in s["feed"]]
    for title in ("Surge Predicted", "Outreach Approved", "Staff Confirmed", "Shift Active",
                  "Capacity Recalculated", "Futures Diverge"):  # fmt: skip
        assert title in titles


def test_confirmation_round_trip_is_under_200ms(client):
    state = _approve_recommended(client, _reset(client))
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        r = _reply(client, state)
        timings.append((time.perf_counter() - started) * 1000)
        assert r.status_code == 200
        state = r.json()
        assert state["meta"]["recompute_ms"] < 200
    assert max(timings) < 200, timings


def test_level_loading_approval_recalculates(client):
    s = _reset(client)
    ll = next(
        p for p in s["proposals"] if p["kind"] == "level_loading" and p["hospital_id"] == "grady"
    )
    r = client.post(f"/api/runs/{s['run']['run_id']}/proposals/{ll['id']}/approve")
    assert r.status_code == 200
    body = r.json()
    assert any(b["kind"] == "level_loading_active" for b in body["bands"])
    assert body["impact"]["state"] == "intervention_scheduled"
    assert body["run"]["version"] > s["run"]["version"]


# --- errors ---------------------------------------------------------------------------
def test_error_responses(client, clock):
    s = _reset(client)
    run_id = s["run"]["run_id"]
    pid = next(p["id"] for p in s["proposals"] if p["recommended"])
    tick = next(p["tick"] for p in s["proposals"] if p["recommended"])

    assert client.get("/api/runs/nope").status_code == 404
    assert client.post(f"/api/runs/{run_id}/proposals/nope/approve").status_code == 404
    assert client.post("/api/demo/reset", json={"scenario_id": "nope"}).status_code == 422
    early = client.post(f"/api/runs/{run_id}/proposals/{pid}/approve", json={"at_tick": tick - 1})
    assert early.status_code == 409

    s = _approve_recommended(client, s)
    assert client.post(f"/api/runs/{run_id}/proposals/{pid}/approve").status_code == 409
    bad_req = client.post(f"/api/runs/{run_id}/staffing-requests/nope/simulate-reply")
    assert bad_req.status_code == 404
    assert _reply(client, s, reply="NO").status_code == 409
    assert _reply(client, s, staff_id="rn-nobody").status_code == 404
    assert _reply(client, s, staff_id="rn-avery").status_code == 200
    assert _reply(client, s, staff_id="rn-avery").status_code == 409  # duplicate reply
    assert _reply(client, s, reply=" in ").status_code == 200  # normalized

    clock.now += timedelta(minutes=11)
    expired = _reply(client, s)
    assert expired.status_code == 409 and "expired" in expired.json()["detail"]
    state = client.get(f"/api/runs/{run_id}").json()
    assert state["staffing_requests"][0]["status"] == "expired"
    assert state["staffing_requests"][0]["confirmed_staff"] == 2  # earlier replies still count


def test_fully_confirmed_request_rejects_more_replies(client):
    s = _confirm_all(client)
    assert _reply(client, s).status_code == 409


# --- other endpoints ------------------------------------------------------------------
def test_diagnostics_hold_engineering_detail(client):
    s = _confirm_all(client)
    d = client.get(f"/api/runs/{s['run']['run_id']}/diagnostics").json()
    assert d["model_limitations"]
    assert d["scenario_assumptions"]
    assert d["forecast_accuracy"]["samples"] > 0
    assert set(d["track_digests"]) == {"baseline", "triagex"}
    assert len(d["recompute_history_ms"]) == 3
    assert len(d["all_proposals"]) >= len(s["proposals"])


def test_reference_endpoints(client):
    hospitals = client.get("/api/hospitals").json()
    assert {h["id"] for h in hospitals} == {
        "grady", "emory_uh", "emory_midtown", "piedmont", "kennestone"}  # fmt: skip
    scenarios = client.get("/api/scenarios").json()
    assert {s["id"] for s in scenarios} == set(SCENARIOS)


def test_integration_health_never_claims_unbuilt_integrations(client):
    h = client.get("/api/integrations/health").json()
    assert h["simulation"]["status"] == "ok"
    for key in ("database", "ai_primary", "ai_fallback", "messaging"):
        assert h[key]["status"] == "not_implemented"


def test_websocket_pushes_run_updates(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        state = _reset(client)
        update = ws.receive_json()
        assert update == {
            "type": "run.updated",
            "run_id": state["run"]["run_id"],
            "version": state["run"]["version"],
            "reason": "reset",
            "recompute_ms": update["recompute_ms"],
        }
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
