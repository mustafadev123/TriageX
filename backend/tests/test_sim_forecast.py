"""The forecast may only use information available at the current tick."""

import dataclasses

from app.simulation.arrivals import generate_patients
from app.simulation.engine import simulate
from app.simulation.forecast import ObservedState, project_saturation
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.models import Mode, Patient, Track
from app.simulation.scenarios import get_scenario

CUTOFF = 14  # 15:30, before the I-85 pileup begins at tick 16


def test_forecast_ignores_future_arrivals():
    scenario = get_scenario("i85_pileup")
    real = generate_patients(DEFAULT_NETWORK, scenario, 42)
    # Replace every future arrival with a much heavier, different stream.
    past = [p for p in real if p.arrival_tick <= CUTOFF]
    future = [p for p in real if p.arrival_tick > CUTOFF]
    heavy = list(past)
    seq = max(p.seq for p in real) + 1
    for p in future:
        for k in range(3):
            heavy.append(
                Patient(
                    seq,
                    f"{p.id}:x{k}",
                    p.hospital_id,
                    p.arrival_tick,
                    Mode.AMBULANCE,
                    1,
                    p.service_ticks * 2,
                    "surge",
                )
            )
            seq += 1

    a = simulate(DEFAULT_NETWORK, scenario, real, (), Track.BASELINE)
    b = simulate(DEFAULT_NETWORK, scenario, heavy, (), Track.BASELINE)

    for t in range(CUTOFF + 1):
        fa = [r.forecast_saturation_pct for r in a.ticks[t]]
        fb = [r.forecast_saturation_pct for r in b.ticks[t]]
        assert fa == fb, f"forecast at tick {t} changed when only the future changed"
    assert [p for p in a.proposals if p.tick <= CUTOFF] == [
        p for p in b.proposals if p.tick <= CUTOFF
    ]
    # Sanity: the altered future really does change later outcomes.
    assert a.ticks[CUTOFF + 4] != b.ticks[CUTOFF + 4]


def test_observed_state_exposes_no_future_or_scenario_fields():
    names = {f.name for f in dataclasses.fields(ObservedState)}
    assert names == {
        "clock_minutes",
        "occupied",
        "queue_length",
        "staffed_capacity",
        "treatment_spaces",
        "recent_arrivals",
        "expected_service_minutes",
    }


def _state(**overrides):
    base = dict(
        clock_minutes=14 * 60,
        occupied=20,
        queue_length=0,
        staffed_capacity=40,
        treatment_spaces=50,
        recent_arrivals=(2, 2, 2, 2),
        expected_service_minutes=150,
    )
    base.update(overrides)
    return ObservedState(**base)


def test_empty_quiet_hospital_projects_zero():
    assert project_saturation(_state(occupied=0, recent_arrivals=(0, 0, 0, 0))) == 0.0


def test_more_recent_arrivals_raise_the_projection():
    low = project_saturation(_state(recent_arrivals=(1, 1, 1, 1)))
    high = project_saturation(_state(recent_arrivals=(6, 6, 6, 6)))
    assert high > low


def test_projection_counts_existing_queue():
    assert project_saturation(_state(queue_length=15)) > project_saturation(_state())


def test_projection_is_pure():
    s = _state(queue_length=5)
    assert project_saturation(s) == project_saturation(s)
