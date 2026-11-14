"""Shared builders for simulation tests."""

from app.simulation.engine import simulate
from app.simulation.hospitals import Network
from app.simulation.models import Hospital, Mode, Patient, Track
from app.simulation.scenarios import Scenario


def make_hospital(
    hid: str, spaces: int, staff: int, pps: float = 2.0, lat: float = 33.75, lon: float = -84.38
) -> Hospital:
    return Hospital(
        id=hid,
        name=hid,
        short_name=hid,
        lat=lat,
        lon=lon,
        treatment_spaces=spaces,
        base_staff=staff,
        patients_per_staff=pps,
        base_arrivals_per_hour=0.0,
        ambulance_fraction=0.0,
        expected_service_minutes=60,
    )


def make_scenario(horizon: int = 12) -> Scenario:
    return Scenario(
        id="unit",
        name="Unit",
        description="hand-built",
        start_clock_minutes=12 * 60,
        surge_onset_tick=0,
        demo_window_start_tick=0,
        horizon_ticks=horizon,
        demo_window_ticks=min(horizon, 24),
    )


def make_patient(
    seq: int,
    hospital_id: str,
    tick: int,
    service: int,
    acuity: int = 2,
    mode: Mode = Mode.AMBULANCE,
) -> Patient:
    return Patient(seq, f"p{seq}", hospital_id, tick, mode, acuity, service, "baseline")


def run(network: Network, scenario: Scenario, patients, interventions=(), track=None):
    track = track or (Track.TRIAGEX if interventions else Track.BASELINE)
    return simulate(network, scenario, patients, interventions, track)
