import subprocess
import sys
from pathlib import Path

import pytest

from app.simulation.arrivals import generate_patients
from app.simulation.comparison import run_comparison, track_digest
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.interventions import StaffingIntervention
from app.simulation.scenarios import SCENARIOS, get_scenario

BACKEND = Path(__file__).resolve().parents[1]
INTERVENTION = (StaffingIntervention("s1", "grady", approved_tick=18, confirmed_staff=3),)


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_same_inputs_replay_identically(scenario_id):
    a = run_comparison(scenario_id, 7, INTERVENTION)
    b = run_comparison(scenario_id, 7, INTERVENTION)
    assert a.patients == b.patients
    assert track_digest(a.baseline) == track_digest(b.baseline)
    assert track_digest(a.triagex) == track_digest(b.triagex)
    assert a.deltas == b.deltas
    assert a.markers == b.markers


def test_different_seeds_produce_different_arrivals():
    scenario = get_scenario("i85_pileup")
    assert generate_patients(DEFAULT_NETWORK, scenario, 1) != generate_patients(
        DEFAULT_NETWORK, scenario, 2
    )


def test_patient_stream_is_ordered_with_unique_seq():
    patients = generate_patients(DEFAULT_NETWORK, get_scenario("i85_pileup"), 42)
    assert [p.seq for p in patients] == list(range(len(patients)))
    assert [p.arrival_tick for p in patients] == sorted(p.arrival_tick for p in patients)
    assert len({p.id for p in patients}) == len(patients)


def test_replay_is_independent_of_python_hash_seed():
    code = (
        "from app.simulation.comparison import run_comparison, track_digest;"
        "from app.simulation.interventions import StaffingIntervention as S;"
        "c = run_comparison('i85_pileup', 42, (S('s1', 'grady', 18, 3),));"
        "print(track_digest(c.baseline), track_digest(c.triagex))"
    )
    outputs = set()
    for hash_seed in ("1", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=BACKEND,
            env={
                "PYTHONHASHSEED": hash_seed,
                "PYTHONPATH": str(BACKEND),
                "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
            },
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1
