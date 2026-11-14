"""One surge, two futures: Track B may differ from Track A only after an intervention acts."""

import pytest

from app.simulation.comparison import first_divergence_tick, run_comparison, track_digest
from app.simulation.engine import simulate
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.interventions import (
    LevelLoadingIntervention,
    StaffingIntervention,
    interventions_from_proposal,
)
from app.simulation.models import Track
from app.simulation.scenarios import SCENARIOS, get_scenario

SEED = 42


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_tracks_identical_without_interventions(scenario_id):
    c = run_comparison(scenario_id, SEED)
    assert c.first_divergence_tick is None
    assert track_digest(c.baseline) == track_digest(c.triagex)
    assert c.baseline_metrics == c.triagex_metrics
    assert all(d.delta == 0 for d in c.deltas.values())


def test_tracks_share_the_same_arrival_stream():
    c = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 3),))

    def fingerprint(track):
        return [
            (
                r.seq,
                r.patient_id,
                r.intended_hospital_id,
                r.original_arrival_tick,
                r.mode,
                r.acuity,
                r.service_ticks,
            )
            for r in track.patients
        ]

    expected = [
        (p.seq, p.id, p.hospital_id, p.arrival_tick, p.mode, p.acuity, p.service_ticks)
        for p in c.patients
    ]
    assert fingerprint(c.baseline) == expected
    assert fingerprint(c.triagex) == expected


def test_baseline_track_rejects_interventions():
    scenario = get_scenario("i85_pileup")
    with pytest.raises(ValueError):
        simulate(
            DEFAULT_NETWORK,
            scenario,
            (),
            (StaffingIntervention("s", "grady", 18, 3),),
            Track.BASELINE,
        )


def test_staffing_has_no_effect_before_lead_time():
    iv = StaffingIntervention("s", "grady", approved_tick=18, confirmed_staff=3)
    c = run_comparison("i85_pileup", SEED, (iv,))
    assert iv.effective_tick == 21  # 45 simulated minutes later
    for t in range(iv.effective_tick):
        assert c.baseline.ticks[t] == c.triagex.ticks[t], f"tick {t} changed before activation"
    assert c.first_divergence_tick is not None
    assert c.first_divergence_tick >= iv.effective_tick


def test_queue_is_not_reduced_at_confirmation_time():
    iv = StaffingIntervention("s", "grady", approved_tick=19, confirmed_staff=3)
    c = run_comparison("i85_pileup", SEED, (iv,))
    a, b = c.baseline.hospital_series("grady"), c.triagex.hospital_series("grady")
    assert a[19].queue_length > 0, "test needs a queue at approval time"
    for t in range(iv.approved_tick, iv.effective_tick):
        assert b[t].queue_length == a[t].queue_length
        assert b[t].staff_on_duty == a[t].staff_on_duty


def test_level_loading_has_no_effect_before_activation():
    iv = LevelLoadingIntervention("ll", "grady", approved_tick=18)
    c = run_comparison("i85_pileup", SEED, (iv,))
    assert c.first_divergence_tick is None or c.first_divergence_tick >= iv.effective_tick


def test_recalculation_after_additional_confirmations():
    one = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 1),))
    three = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 3),))
    for t in range(21):
        assert one.triagex.ticks[t] == three.triagex.ticks[t]
    assert first_divergence_tick(one.triagex, three.triagex) is not None
    assert three.triagex.hospital_series("grady")[21].staff_on_duty == 16
    assert one.triagex.hospital_series("grady")[21].staff_on_duty == 14


def test_staffing_changes_only_the_authorized_hospital():
    c = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 3),))
    for hid in c.triagex.hospital_ids:
        base = DEFAULT_NETWORK.get(hid)
        for row in c.triagex.hospital_series(hid):
            if hid == "grady" and 21 <= row.tick < 37:
                assert row.staff_on_duty == base.base_staff + 3
            else:
                assert row.staff_on_duty == base.base_staff


def test_zero_confirmations_changes_nothing():
    c = run_comparison("i85_pileup", SEED, (StaffingIntervention("s", "grady", 18, 0),))
    assert c.first_divergence_tick is None


def test_proposals_match_until_divergence():
    baseline = run_comparison("i85_pileup", SEED)
    proposal = next(
        p
        for p in baseline.baseline.proposals
        if p.hospital_id == "grady" and p.tick >= 16 and p.kind == "staffing"
    )
    ivs = interventions_from_proposal(proposal, proposal.tick + 1, 3)
    c = run_comparison("i85_pileup", SEED, ivs)
    cutoff = c.first_divergence_tick
    assert cutoff is not None
    assert [p for p in c.baseline.proposals if p.tick < cutoff] == [
        p for p in c.triagex.proposals if p.tick < cutoff
    ]


def test_proposal_cannot_be_approved_before_it_exists():
    proposal = run_comparison("i85_pileup", SEED).baseline.proposals[0]
    with pytest.raises(ValueError):
        interventions_from_proposal(proposal, proposal.tick - 1, 2)
