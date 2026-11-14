"""Hand-built networks with exactly known answers."""

from app.simulation.hospitals import Network
from app.simulation.interventions import StaffingIntervention
from app.simulation.metrics import summarize
from app.simulation.models import Mode
from tests.sim_helpers import make_hospital, make_patient, make_scenario, run


def test_ambulance_handoff_waits_until_a_space_frees():
    net = Network((make_hospital("a", spaces=2, staff=1, pps=2.0),))
    patients = [make_patient(i, "a", tick=0, service=4) for i in range(3)]
    result = run(net, make_scenario(horizon=12), patients)

    placements = [r.placement_tick for r in result.patients]
    assert placements == [0, 0, 4]
    m = summarize(result)
    assert m.ambulance_patients == 3
    assert m.ambulance_handoff_hours == 1.0  # one crew held for 60 minutes
    assert m.mean_handoff_minutes == 20.0
    assert m.max_wait_minutes == 60.0
    assert m.ambulance_hours_lost == 1.0  # no reroutes, so no extra travel


def test_walk_ins_do_not_count_as_handoff_time():
    net = Network((make_hospital("a", spaces=1, staff=1, pps=1.0),))
    patients = [
        make_patient(0, "a", 0, 4, mode=Mode.WALK_IN),
        make_patient(1, "a", 0, 4, mode=Mode.WALK_IN),
    ]
    m = summarize(run(net, make_scenario(), patients))
    assert m.mean_wait_minutes == 30.0
    assert m.ambulance_handoff_hours == 0.0


def test_acuity_orders_the_queue():
    net = Network((make_hospital("a", spaces=1, staff=1, pps=1.0),))
    patients = [
        make_patient(0, "a", 0, 2, acuity=1),
        make_patient(1, "a", 1, 2, acuity=3),
        make_patient(2, "a", 1, 2, acuity=1),
    ]
    result = run(net, make_scenario(), patients)
    by_seq = {r.seq: r.placement_tick for r in result.patients}
    assert by_seq == {0: 0, 2: 2, 1: 4}


def test_unplaced_patients_get_censored_waits():
    net = Network((make_hospital("a", spaces=1, staff=1, pps=1.0),))
    patients = [make_patient(0, "a", 0, 50), make_patient(1, "a", 2, 1)]
    m = summarize(run(net, make_scenario(horizon=10), patients))
    assert m.patients_waiting_at_end == 1
    assert m.max_wait_minutes == (10 - 2) * 15


def test_diversion_reroutes_non_critical_ambulances_and_counts_travel():
    near = make_hospital("full", spaces=1, staff=1, pps=1.0, lat=33.75, lon=-84.38)
    far = make_hospital("open", spaces=10, staff=10, pps=1.0, lat=33.80, lon=-84.38)
    net = Network((near, far))
    patients = [
        make_patient(0, "full", 0, 40),
        make_patient(1, "full", 0, 40),
        make_patient(2, "full", 0, 40),
        make_patient(3, "full", 3, 2, acuity=2),  # arrives while "full" is diverting
        make_patient(4, "full", 3, 2, acuity=1),  # critical: never diverted
    ]
    result = run(net, make_scenario(horizon=12), patients)
    full_series = result.hospital_series("full")
    assert not full_series[0].diverting
    assert full_series[1].diverting  # breached with an ambulance backlog for 2 ticks

    rec = {r.seq: r for r in result.patients}
    travel = net.travel[0][1]
    assert rec[3].final_hospital_id == "open"
    assert rec[3].reroute_reason == "diversion"
    assert rec[3].ed_arrival_tick == 3 + travel
    assert rec[3].placement_tick == 3 + travel
    assert rec[4].final_hospital_id == "full"
    assert rec[4].reroute_reason is None

    m = summarize(result)
    assert m.diverted_ambulances == 1
    assert m.diversion_onsets == 1
    assert m.ambulance_reroute_travel_hours == travel * 15 / 60
    assert m.ambulance_hours_lost == m.ambulance_handoff_hours + m.ambulance_reroute_travel_hours


def test_no_diversion_when_every_hospital_is_diverting():
    net = Network((make_hospital("solo", spaces=1, staff=1, pps=1.0),))
    patients = [make_patient(i, "solo", 0, 40) for i in range(3)]
    patients.append(make_patient(3, "solo", 4, 1, acuity=3))
    result = run(net, make_scenario(), patients)
    assert all(r.final_hospital_id == "solo" for r in result.patients)


def test_staffing_lead_time_in_isolation():
    net = Network((make_hospital("a", spaces=4, staff=1, pps=2.0),))
    patients = [make_patient(i, "a", 0, 20) for i in range(4)]
    iv = StaffingIntervention("s", "a", approved_tick=1, confirmed_staff=1)
    result = run(net, make_scenario(horizon=12), patients, (iv,))
    placements = sorted(r.placement_tick for r in result.patients)
    assert placements == [0, 0, 4, 4]  # approved at tick 1, staff on the floor at tick 4
