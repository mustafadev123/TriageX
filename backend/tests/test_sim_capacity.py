"""Capacity, conservation, and 'every metric comes from simulation state'."""

import pytest

from app.simulation.comparison import run_comparison
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.interventions import (
    LevelLoadingIntervention,
    StaffingIntervention,
    validate_interventions,
)
from app.simulation.metrics import summarize
from app.simulation.models import TICK_MINUTES, Mode
from app.simulation.scenarios import SCENARIOS

SEED = 42
DEMO_IVS = (
    StaffingIntervention("s", "grady", 18, 3),
    LevelLoadingIntervention("ll", "grady", 18),
)


def _all_tracks():
    for sid in sorted(SCENARIOS):
        c = run_comparison(sid, SEED, DEMO_IVS)
        yield sid, c.baseline
        yield sid, c.triagex


def test_staffing_is_capped_by_physical_treatment_spaces():
    grady = DEFAULT_NETWORK.get("grady")
    c = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 10),))
    active = [r for r in c.triagex.hospital_series("grady") if 21 <= r.tick < 37]
    assert all(r.staff_on_duty == grady.base_staff + 10 for r in active)
    assert all(r.staffed_capacity == grady.treatment_spaces for r in active)
    effect = c.intervention_effects[0]
    assert effect.staffed_capacity_before == 45
    assert effect.staffed_capacity_with_staff == 56
    assert (effect.usable_staff, effect.unusable_staff) == (3, 7)


def test_extra_staff_beyond_space_limit_adds_nothing():
    three = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 3),))
    ten = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 10),))
    for a, b in zip(three.triagex.ticks, ten.triagex.ticks, strict=True):
        for ra, rb in zip(a, b, strict=True):
            assert (ra.occupied, ra.queue_length, ra.staffed_capacity) == (
                rb.occupied,
                rb.queue_length,
                rb.staffed_capacity,
            )


@pytest.mark.parametrize(("sid", "track"), list(_all_tracks()))
def test_state_invariants(sid, track):
    for row in (r for tick in track.ticks for r in tick):
        assert row.queue_length >= 0
        assert row.ambulance_queue_length >= 0
        assert row.ambulance_queue_length <= row.queue_length
        assert 0 <= row.occupied <= row.treatment_spaces
        if row.placements:
            assert row.occupied <= row.staffed_capacity
        assert row.staffed_capacity <= row.treatment_spaces


@pytest.mark.parametrize(("sid", "track"), list(_all_tracks()))
def test_tick_state_matches_patient_ledger(sid, track):
    """Occupancy and queues reported per tick are reproducible from patient records."""
    by_hospital = {hid: [] for hid in track.hospital_ids}
    for r in track.patients:
        if r.ed_arrival_tick is not None and r.ed_arrival_tick < track.horizon_ticks:
            by_hospital[r.final_hospital_id].append(r)
    for hid, records in by_hospital.items():
        for row in track.hospital_series(hid):
            t = row.tick
            occupied = sum(
                1
                for r in records
                if r.placement_tick is not None
                and r.placement_tick <= t < r.placement_tick + r.service_ticks
            )
            queued = sum(
                1
                for r in records
                if r.ed_arrival_tick <= t and (r.placement_tick is None or r.placement_tick > t)
            )
            assert (row.occupied, row.queue_length) == (occupied, queued), (hid, t)


@pytest.mark.parametrize(("sid", "track"), list(_all_tracks()))
def test_patients_are_conserved_and_ordered(sid, track):
    for r in track.patients:
        if r.ed_arrival_tick is None:
            assert r.original_arrival_tick >= track.horizon_ticks
            continue
        assert r.ed_arrival_tick == r.original_arrival_tick + r.extra_travel_ticks
        if r.placement_tick is not None:
            assert r.placement_tick >= r.ed_arrival_tick
    m = summarize(track)
    assert m.patients_arrived == m.patients_placed + m.patients_waiting_at_end


def test_summary_metrics_recompute_from_ledger():
    c = run_comparison("i85_pileup", SEED, DEMO_IVS)
    track = c.triagex
    horizon = track.horizon_ticks
    eligible = [
        r
        for r in track.patients
        if r.ed_arrival_tick is not None and 0 <= r.ed_arrival_tick < horizon
    ]
    waits = [
        ((r.placement_tick if r.placement_tick is not None else horizon) - r.ed_arrival_tick)
        * TICK_MINUTES
        for r in eligible
    ]
    amb = [w for r, w in zip(eligible, waits, strict=True) if r.mode is Mode.AMBULANCE]
    m = c.triagex_metrics
    assert m.mean_wait_minutes == round(sum(waits) / len(waits), 2)
    assert m.ambulance_handoff_hours == round(sum(amb) / 60, 2)
    assert m.transfers == sum(1 for r in track.patients if r.reroute_reason == "level_loading")
    assert m.diverted_ambulances == sum(
        1
        for r in track.patients
        if r.reroute_reason == "diversion" and 0 <= r.original_arrival_tick < horizon
    )
    peak = max(row.saturation_pct for tick in track.ticks for row in tick)
    assert m.peak_saturation_pct == peak


def test_level_loading_respects_its_constraints():
    iv = LevelLoadingIntervention("ll", "grady", approved_tick=18)
    c = run_comparison("i85_pileup", SEED, (iv,))
    redirected = [r for r in c.triagex.patients if r.intervention_id == "ll"]
    assert redirected, "expected at least one redirect during the pileup"
    per_tick: dict[int, int] = {}
    for r in redirected:
        assert r.mode is Mode.AMBULANCE and r.acuity >= iv.min_acuity
        assert iv.effective_tick <= r.original_arrival_tick < iv.end_tick
        assert r.final_hospital_id != "grady"
        per_tick[r.original_arrival_tick] = per_tick.get(r.original_arrival_tick, 0) + 1
        prev = c.triagex.hospital_series(r.final_hospital_id)[r.original_arrival_tick - 1]
        assert prev.forecast_saturation_pct < iv.receiver_forecast_limit_pct
        assert not prev.diverting
    assert max(per_tick.values()) <= iv.max_redirects_per_tick


def test_invalid_interventions_are_rejected():
    for bad in (
        StaffingIntervention("x", "nowhere", 5, 1),
        StaffingIntervention("x", "grady", -1, 1),
        StaffingIntervention("x", "grady", 5, 99),
        StaffingIntervention("x", "grady", 500, 1),
    ):
        with pytest.raises(ValueError):
            validate_interventions((bad,), DEFAULT_NETWORK, 96)
    dup = (StaffingIntervention("x", "grady", 5, 1), StaffingIntervention("x", "grady", 6, 1))
    with pytest.raises(ValueError):
        validate_interventions(dup, DEFAULT_NETWORK, 96)
