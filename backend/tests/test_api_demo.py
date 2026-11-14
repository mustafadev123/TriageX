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


APPROVAL_DELAY_TICKS = 1  # the operator approves 15 simulated minutes after the proposal


def _approve_recommended(client, state, delay=APPROVAL_DELAY_TICKS):
    card = next(p for p in state["proposals"] if p["recommended"])
    r = client.post(
        f"/api/runs/{state['run']['run_id']}/proposals/{card['id']}/approve",
        json={"at_tick": card["tick"] + delay},
    )
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


def test_lead_time_starts_at_operator_approval_not_at_proposal(client):
    """I-85: proposal 16:15, approval 16:30, staff on the floor 17:15."""
    s = _approve_recommended(client, _reset(client), delay=1)
    for _ in range(3):
        s = _reply(client, s).json()
    req = s["staffing_requests"][0]
    assert (req["proposal_clock"], req["approved_clock"], req["effective_clock"]) == (
        "16:15", "16:30", "17:15")  # fmt: skip
    assert req["effective_tick"] == req["approved_tick"] + 3 == req["proposal_tick"] + 4

    bands = {b["kind"]: b for b in s["bands"]}
    assert (bands["staff_lead_time"]["start_tick"], bands["staff_lead_time"]["end_tick"]) == (
        req["approved_tick"], req["effective_tick"])  # fmt: skip
    assert bands["shift_active"]["start_tick"] == req["effective_tick"]

    feed = {}
    for e in s["feed"]:  # first in-window occurrence of each title at the focus hospital
        if e["in_demo_window"] and e["hospital_id"] in (None, "grady"):
            feed.setdefault(e["title"], e)
    assert feed["Surge Predicted"]["clock"] == "16:15"
    assert feed["Outreach Approved"]["clock"] == "16:30"
    assert "17:15" in feed["Outreach Approved"]["detail"]
    assert feed["Staff Confirmed"]["clock"] == "16:30"
    assert feed["Shift Active"]["clock"] == "17:15"

    markers = {(m["kind"], m["clock"]) for m in s["markers"]}
    assert ("approval", "16:30") in markers and ("shift_active", "17:15") in markers

    phases = {p["clock"]: p["phase"] for p in s["curves"]["points"]}
    assert phases["16:15"] == "before_intervention"
    assert phases["16:30"] == phases["17:00"] == "staff_en_route"
    assert phases["17:15"] == "shift_active"

    grady = next(h for h in s["series"] if h["hospital_id"] == "grady")["triagex"]
    assert grady[req["effective_tick"] - 1]["staff_on_duty"] == 13
    assert grady[req["effective_tick"]]["staff_on_duty"] == 16
    assert s["divergence"]["tick"] >= req["effective_tick"]


def test_later_approval_moves_activation_later(client):
    early = _approve_recommended(client, _reset(client), delay=0)
    late = _approve_recommended(client, _reset(client), delay=4)
    e, lt = early["staffing_requests"][0], late["staffing_requests"][0]
    assert lt["approved_tick"] - e["approved_tick"] == 4
    assert lt["effective_tick"] - e["effective_tick"] == 4


def test_each_confirmation_recomputes_track_b_only(client):
    s = _approve_recommended(client, _reset(client))
    run_id = s["run"]["run_id"]
    digests = [client.get(f"/api/runs/{run_id}/diagnostics").json()["track_digests"]]
    for _ in range(3):
        s = _reply(client, s).json()
        digests.append(client.get(f"/api/runs/{run_id}/diagnostics").json()["track_digests"])
    assert len({d["baseline"] for d in digests}) == 1  # baseline never changes
    assert len({d["triagex"] for d in digests}) == 4  # every confirmation changes Track B
    assert digests[0]["baseline"] == digests[0]["triagex"]  # identical before confirmations


def test_impact_is_computed_from_the_returned_series(client):
    """The hero numbers can be re-derived from the series in the same response."""
    s = _confirm_all(client)
    start, end = (
        s["timeline"]["demo_window"]["start_tick"],
        s["timeline"]["demo_window"]["end_tick"],
    )

    def ambulance_hours(track):
        waiting = sum(
            h[track][t]["ambulances_waiting"] for h in s["series"] for t in range(start, end)
        )
        return round(waiting * 15 / 60, 2)

    primary = s["impact"]["primary"]
    assert primary["baseline"] == ambulance_hours("baseline")
    assert primary["triagex"] == ambulance_hours("triagex")
    focus = next(h for h in s["series"] if h["hospital_id"] == s["curves"]["focus_hospital_id"])
    for p in s["curves"]["points"]:
        t = p["tick"]
        assert p["baseline_focus_saturation_pct"] == focus["baseline"][t]["saturation_pct"]
        assert p["triagex_patients_waiting"] == sum(
            h["triagex"][t]["queue_length"] for h in s["series"]
        )


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
    assert all("via simulated reply" in e["detail"] for e in confirmed)
    # Nothing in the payload claims a real message was sent or received.
    assert not any(e["origin"] == "webhook" for e in s["feed"])
    assert not any("message sent" in e["title"].lower() for e in s["feed"])
    assert "Chert" not in str(s["feed"])
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
    r = client.post(
        f"/api/runs/{s['run']['run_id']}/proposals/{ll['id']}/approve",
        json={"at_tick": ll["tick"] + 1},
    )
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
    nope = client.post(f"/api/runs/{run_id}/proposals/nope/approve", json={"at_tick": tick})
    assert nope.status_code == 404
    missing_tick = client.post(f"/api/runs/{run_id}/proposals/{pid}/approve")
    assert missing_tick.status_code == 422  # the approval time is never assumed
    assert client.post("/api/demo/reset", json={"scenario_id": "nope"}).status_code == 422
    early = client.post(f"/api/runs/{run_id}/proposals/{pid}/approve", json={"at_tick": tick - 1})
    assert early.status_code == 409

    s = _approve_recommended(client, s)
    again = client.post(f"/api/runs/{run_id}/proposals/{pid}/approve", json={"at_tick": tick + 2})
    assert again.status_code == 409
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
    # No DATABASE_URL in tests: implemented but not configured, never "ok".
    assert h["database"]["status"] == "not_configured"
    for key in ("ai_primary", "ai_fallback"):
        assert h[key]["status"] == "not_implemented"
    # Chert is implemented; without credentials it is not configured, never "ok".
    assert h["messaging"]["status"] == "not_configured"
    assert "CHERT_SIGNING_SECRET" in h["messaging"]["detail"]


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


def test_feed_includes_proposed_and_en_route_steps(client):
    s = _reset(client)
    proposed = [e for e in s["feed"] if e["title"] == "Outreach Proposed"]
    assert len(proposed) == 1 and proposed[0]["clock"] == "16:15"
    assert proposed[0]["detail"] == "Call in 3 staff at Grady"
    assert not any(e["title"] == "Staff En Route" for e in s["feed"])
    s = _approve_recommended(client, s)
    assert not any(e["title"] == "Staff En Route" for e in s["feed"])  # nobody confirmed yet
    s = _reply(client, s).json()
    en_route = [e for e in s["feed"] if e["title"] == "Staff En Route"]
    assert len(en_route) == 1 and en_route[0]["clock"] == "16:30"
    assert "shift starts 17:15" in en_route[0]["detail"]


def test_provenance_reports_git_state(client):
    body = client.get("/api/meta/provenance").json()
    assert body["engine_version"]
    assert isinstance(body["entire_checkpoints"], list)
    assert body["entire_checkpoint_count"] == len(body["entire_checkpoints"])
