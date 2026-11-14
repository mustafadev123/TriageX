"""Scenario-level behaviour. Assertions are about mechanisms, never target improvements."""

import dataclasses

import pytest

from app.simulation.arrivals import generate_patients
from app.simulation.cli import main as cli_main
from app.simulation.cli import pick_surge_proposal
from app.simulation.comparison import run_comparison
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.interventions import interventions_from_proposal
from app.simulation.metrics import summarize
from app.simulation.models import DEMO_WINDOW_TICKS, Status
from app.simulation.scenarios import SCENARIOS, get_scenario

SEED = 42


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenario_definitions_are_well_formed(scenario_id):
    s = get_scenario(scenario_id)
    start, end = s.demo_window
    assert end - start == DEMO_WINDOW_TICKS == 24
    assert 0 <= start < end <= s.horizon_ticks
    assert 0 <= s.surge_onset_tick < s.horizon_ticks
    assert s.assumptions
    for b in s.bursts:
        assert b.hospital_id in DEFAULT_NETWORK.index
        assert sum(b.expected_at(t) for t in range(s.horizon_ticks)) == pytest.approx(
            b.expected_patients
        )


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_surge_adds_demand_compared_with_a_calm_day(scenario_id):
    s = get_scenario(scenario_id)
    calm = dataclasses.replace(
        s,
        demand_windows=(),
        bursts=(),
        service_multiplier=1.0,
        walk_in_acuity_mix=None,
        ambulance_fraction_delta=0.0,
    )
    totals = []
    for scenario in (s, calm):
        totals.append(
            sum(
                1
                for p in generate_patients(DEFAULT_NETWORK, scenario, SEED)
                if 0 <= p.arrival_tick < s.horizon_ticks
            )
        )
    assert totals[0] > totals[1]


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenarios_raise_pressure_and_proposals(scenario_id):
    s = get_scenario(scenario_id)
    c = run_comparison(s, SEED)
    statuses = {row.status for tick in c.baseline.ticks for row in tick}
    assert Status.PRESSURE in statuses or Status.BREACH in statuses
    assert pick_surge_proposal(c.baseline.proposals, s.surge_onset_tick) is not None


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_full_demo_flow_runs_for_every_scenario(scenario_id):
    s = get_scenario(scenario_id)
    proposal = pick_surge_proposal(run_comparison(s, SEED).baseline.proposals, s.surge_onset_tick)
    ivs = interventions_from_proposal(proposal, proposal.tick + 1, proposal.recommended_staff)
    c = run_comparison(s, SEED, ivs)
    earliest = min(iv.effective_tick for iv in ivs)
    assert c.first_divergence_tick is None or c.first_divergence_tick >= earliest
    kinds = {m.kind for m in c.markers}
    assert {"surge_onset", "demo_window_start", "approval", "shift_active"} <= kinds
    # Track B metrics are whatever the model produced; they only need to be internally valid.
    for m in (c.triagex_metrics, c.window_triagex_metrics):
        assert m.patients_arrived == m.patients_placed + m.patients_waiting_at_end


def test_i85_baseline_is_a_credible_surge():
    c = run_comparison("i85_pileup", SEED)
    grady = summarize(c.baseline, "grady", c.scenario.demo_window)
    assert grady.peak_saturation_pct >= 100
    assert grady.diversion_onsets >= 1  # baseline reacts with ordinary diversion
    assert grady.peak_saturation_tick >= c.scenario.surge_onset_tick
    pre_surge = summarize(c.baseline, "grady", (0, c.scenario.surge_onset_tick))
    assert pre_surge.hospital_ticks_in_breach == 0
    surge_patients = [p for p in c.patients if p.source == "surge"]
    assert surge_patients
    assert min(p.arrival_tick for p in surge_patients) >= c.scenario.surge_onset_tick


def test_i85_staffing_proposal_targets_grady_after_onset():
    c = run_comparison("i85_pileup", SEED)
    p = pick_surge_proposal(c.baseline.proposals, c.scenario.surge_onset_tick)
    assert p.hospital_id == "grady"
    assert p.recommended_staff == 3  # exactly fills Grady's 56 synthetic spaces
    assert p.forecast_saturation_pct >= 90


def test_cli_prints_a_report(capsys):
    assert cli_main(["i85_pileup", "--seed", str(SEED)]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DATA" in out
    assert "First divergence" in out
