"""Deterministic 15-minute tick engine.

`simulate` is a pure function of (network, scenario, patients, interventions, track).
It uses no randomness and no wall-clock time, so the same inputs always produce the
same outputs. Both tracks share the same rules, including reactive diversion; the only
difference is the list of approved interventions.

Order of operations within each tick t:
  1. Apply staffing (base staff plus any confirmed staff whose shift is active at t).
  2. Release patients whose treatment ends at t.
  3. Land rerouted ambulances whose extra travel ends at t.
  4. Route new arrivals: diversion (both tracks); active level-loading (Track B only)
     chooses forecast-aware receivers for eligible ambulances.
  5. Place waiting patients by (acuity, ED arrival tick, seq) into free staffed spaces.
  6. Observe: saturation, diversion state, two-hour forecast, proposals.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Sequence

from app.simulation.forecast import (
    PROPOSAL_REARM_PCT,
    PROPOSAL_THRESHOLD_PCT,
    ObservedState,
    project_saturation,
)
from app.simulation.hospitals import Network
from app.simulation.interventions import (
    Intervention,
    LevelLoadingIntervention,
    StaffingIntervention,
    validate_interventions,
)
from app.simulation.models import (
    BREACH_THRESHOLD_PCT,
    TICK_MINUTES,
    EventKind,
    Hospital,
    HospitalTick,
    Mode,
    Patient,
    PatientRecord,
    Proposal,
    RerouteReason,
    SimEvent,
    Track,
    TrackResult,
    classify_saturation,
)
from app.simulation.scenarios import Scenario

DIVERSION_TRIGGER_TICKS = 2  # consecutive breached ticks with an ambulance backlog
DIVERSION_RELEASE_PCT = 90.0
MAX_RECOMMENDED_STAFF = 4
TRAILING_HISTORY = 4


class _State:
    __slots__ = (
        "hospital", "staff", "capacity", "occupied", "ends", "queue", "amb_queue", "diverting",
        "breach_streak", "last_forecast", "proposal_armed", "history", "arrivals", "placements",
        "completions", "diverted_out", "diverted_in", "transfers_out", "transfers_in",
        "redirects_this_tick", "accepted_this_tick",
    )  # fmt: skip

    def __init__(self, hospital: Hospital):
        self.hospital = hospital
        self.staff = hospital.base_staff
        self.capacity = hospital.staffed_capacity(hospital.base_staff)
        self.occupied = 0
        self.ends: dict[int, int] = defaultdict(int)
        self.queue: list[tuple[int, int, int, int]] = []  # (acuity, ed_arrival, seq, idx)
        self.amb_queue = 0
        self.diverting = False
        self.breach_streak = 0
        self.last_forecast = 0.0
        self.proposal_armed = True
        self.history: list[int] = []
        self.reset_counters()

    def reset_counters(self) -> None:
        self.arrivals = self.placements = self.completions = 0
        self.diverted_out = self.diverted_in = self.transfers_out = self.transfers_in = 0
        self.redirects_this_tick = self.accepted_this_tick = 0


def simulate(
    network: Network,
    scenario: Scenario,
    patients: Sequence[Patient],
    interventions: Sequence[Intervention] = (),
    track: Track = Track.BASELINE,
) -> TrackResult:
    interventions = tuple(interventions)
    if track is Track.BASELINE and interventions:
        raise ValueError("The baseline track cannot receive TriageX interventions")
    horizon = scenario.horizon_ticks
    validate_interventions(interventions, network, horizon)

    ordered = sorted(patients, key=lambda p: (p.arrival_tick, p.seq))
    if len({p.seq for p in ordered}) != len(ordered):
        raise ValueError("Patient seq values must be unique")
    for p in ordered:
        if p.hospital_id not in network.index:
            raise ValueError(f"Patient {p.id}: unknown hospital '{p.hospital_id}'")

    start_tick = min(0, ordered[0].arrival_tick) if ordered else 0
    records = [
        PatientRecord(p.seq, p.id, p.mode, p.acuity, p.hospital_id, p.arrival_tick, p.service_ticks)
        for p in ordered
    ]
    fresh: dict[int, list[int]] = defaultdict(list)
    for idx, p in enumerate(ordered):
        if p.arrival_tick < horizon:
            fresh[p.arrival_tick].append(idx)
    landing: dict[int, list[int]] = defaultdict(list)

    states = [_State(h) for h in network.hospitals]
    staffing: dict[int, list[StaffingIntervention]] = defaultdict(list)
    level_loading: dict[int, list[LevelLoadingIntervention]] = defaultdict(list)
    for iv in sorted(interventions, key=lambda iv: iv.id):
        i = network.index[iv.hospital_id]
        if isinstance(iv, StaffingIntervention):
            staffing[i].append(iv)
        else:
            level_loading[i].append(iv)

    ticks_out: list[list[HospitalTick]] = []
    events: list[SimEvent] = []
    proposals: list[Proposal] = []

    def emit(t: int, kind: EventKind, hospital_id: str, **detail: object) -> None:
        if t >= 0:
            events.append(SimEvent(t, kind, hospital_id, dict(detail)))

    def enqueue(i: int, idx: int) -> None:
        s, rec = states[i], records[idx]
        heapq.heappush(s.queue, (rec.acuity, rec.ed_arrival_tick, rec.seq, idx))
        s.arrivals += 1
        if rec.mode is Mode.AMBULANCE:
            s.amb_queue += 1

    for t in range(start_tick, horizon):
        # 1. Staffing
        for i, s in enumerate(states):
            s.reset_counters()
            s.staff = s.hospital.base_staff
            for iv in staffing[i]:
                if iv.active(t):
                    s.staff += iv.confirmed_staff
                if iv.confirmed_staff > 0 and t == iv.effective_tick:
                    emit(
                        t,
                        EventKind.STAFF_SHIFT_ACTIVE,
                        s.hospital.id,
                        intervention_id=iv.id,
                        added_staff=iv.confirmed_staff,
                    )
                if iv.confirmed_staff > 0 and t == iv.end_tick:
                    emit(t, EventKind.STAFF_SHIFT_ENDED, s.hospital.id, intervention_id=iv.id)
            for iv in level_loading[i]:
                if t == iv.effective_tick:
                    emit(t, EventKind.LEVEL_LOADING_ACTIVE, s.hospital.id, intervention_id=iv.id)
                if t == iv.end_tick:
                    emit(t, EventKind.LEVEL_LOADING_ENDED, s.hospital.id, intervention_id=iv.id)
            s.capacity = s.hospital.staffed_capacity(s.staff)

        # 2. Completions
        for s in states:
            done = s.ends.pop(t, 0)
            s.occupied -= done
            s.completions = done

        # 3. Rerouted ambulances arrive at their new destination
        for idx in landing.pop(t, ()):
            enqueue(network.index[records[idx].final_hospital_id], idx)

        # 4. New arrivals
        for idx in fresh.pop(t, ()):
            rec = records[idx]
            origin = network.index[rec.intended_hospital_id]
            so = states[origin]
            target: int | None = None
            reason: RerouteReason | None = None
            via: LevelLoadingIntervention | None = None

            # Level-loading (Track B only) picks a receiver using observed forecasts.
            if rec.mode is Mode.AMBULANCE:
                for iv in level_loading[origin]:
                    if not (iv.active(t) and rec.acuity >= iv.min_acuity):
                        continue
                    if so.redirects_this_tick >= iv.max_redirects_per_tick:
                        continue
                    for j in network.by_distance[origin]:
                        r = states[j]
                        if (
                            not r.diverting
                            and r.last_forecast < iv.receiver_forecast_limit_pct
                            and r.accepted_this_tick < iv.max_redirects_per_tick
                        ):
                            target, via = j, iv
                            break
                    if target is not None:
                        break

            if rec.mode is Mode.AMBULANCE and rec.acuity > 1 and so.diverting:
                # Diversion applies in both tracks. A coordinated receiver chosen above is
                # still counted as a diversion; otherwise the nearest non-diverting ED is used.
                reason = RerouteReason.DIVERSION
                if target is None:
                    target = next(
                        (j for j in network.by_distance[origin] if not states[j].diverting), None
                    )
                    if target is None:
                        reason = None
            elif target is not None:
                reason = RerouteReason.LEVEL_LOADING

            if target is None:
                rec.final_hospital_id = rec.intended_hospital_id
                rec.ed_arrival_tick = t
                enqueue(origin, idx)
                continue

            travel = network.travel[origin][target]
            receiver = states[target]
            rec.final_hospital_id = receiver.hospital.id
            rec.reroute_reason = reason
            rec.extra_travel_ticks = travel
            rec.ed_arrival_tick = t + travel
            if via is not None:
                rec.intervention_id = via.id
                so.redirects_this_tick += 1
                receiver.accepted_this_tick += 1
            if reason is RerouteReason.DIVERSION:
                so.diverted_out += 1
                receiver.diverted_in += 1
                emit(
                    t,
                    EventKind.AMBULANCE_DIVERTED,
                    so.hospital.id,
                    patient_id=rec.patient_id,
                    to=receiver.hospital.id,
                    travel_ticks=travel,
                    intervention_id=via.id if via else None,
                )
            else:
                so.transfers_out += 1
                receiver.transfers_in += 1
                emit(
                    t,
                    EventKind.LEVEL_LOAD_REDIRECT,
                    so.hospital.id,
                    patient_id=rec.patient_id,
                    to=receiver.hospital.id,
                    travel_ticks=travel,
                    intervention_id=via.id,
                )
            if t + travel < horizon:
                landing[t + travel].append(idx)

        # 5. Placement
        for s in states:
            while s.queue and s.occupied < s.capacity:
                *_, idx = heapq.heappop(s.queue)
                rec = records[idx]
                rec.placement_tick = t
                s.occupied += 1
                s.ends[t + rec.service_ticks] += 1
                s.placements += 1
                if rec.mode is Mode.AMBULANCE:
                    s.amb_queue -= 1

        # 6. Observe
        row: list[HospitalTick] = []
        for s in states:
            h = s.hospital
            queue_len = len(s.queue)
            saturation = round((s.occupied + queue_len) / h.treatment_spaces * 100, 2)
            s.history.append(s.arrivals)

            if saturation >= BREACH_THRESHOLD_PCT and s.amb_queue > 0:
                s.breach_streak += 1
            else:
                s.breach_streak = 0
            if not s.diverting and s.breach_streak >= DIVERSION_TRIGGER_TICKS:
                s.diverting = True
                emit(t, EventKind.DIVERSION_START, h.id, saturation_pct=saturation)
            elif s.diverting and saturation < DIVERSION_RELEASE_PCT:
                s.diverting = False
                emit(t, EventKind.DIVERSION_END, h.id, saturation_pct=saturation)

            forecast = project_saturation(
                ObservedState(
                    clock_minutes=scenario.clock_minutes(t),
                    occupied=s.occupied,
                    queue_length=queue_len,
                    staffed_capacity=s.capacity,
                    treatment_spaces=h.treatment_spaces,
                    recent_arrivals=tuple(s.history[-TRAILING_HISTORY:]),
                    expected_service_minutes=h.expected_service_minutes,
                )
            )
            s.last_forecast = forecast

            if t >= 0 and s.proposal_armed and forecast >= PROPOSAL_THRESHOLD_PCT:
                s.proposal_armed = False
                proposals.extend(_proposals_for(scenario, h, s, t, forecast, saturation))
            elif forecast < PROPOSAL_REARM_PCT:
                s.proposal_armed = True

            if t < 0:
                continue
            waits = [(t - rec_tick) * TICK_MINUTES for _, rec_tick, _, _ in s.queue]
            row.append(
                HospitalTick(
                    tick=t,
                    hospital_id=h.id,
                    staff_on_duty=s.staff,
                    staffed_capacity=s.capacity,
                    treatment_spaces=h.treatment_spaces,
                    occupied=s.occupied,
                    queue_length=queue_len,
                    ambulance_queue_length=s.amb_queue,
                    arrivals=s.arrivals,
                    placements=s.placements,
                    completions=s.completions,
                    saturation_pct=saturation,
                    status=classify_saturation(saturation),
                    diverting=s.diverting,
                    diverted_out=s.diverted_out,
                    diverted_in=s.diverted_in,
                    transfers_out=s.transfers_out,
                    transfers_in=s.transfers_in,
                    mean_current_wait_minutes=round(sum(waits) / len(waits), 2) if waits else 0.0,
                    staff_utilization_pct=(
                        round(s.occupied / (s.staff * h.patients_per_staff) * 100, 2)
                        if s.staff > 0
                        else 0.0
                    ),
                    forecast_saturation_pct=forecast,
                )
            )
        if t >= 0:
            ticks_out.append(row)

    return TrackResult(
        track=track,
        scenario_id=scenario.id,
        horizon_ticks=horizon,
        start_tick=start_tick,
        hospital_ids=network.ids,
        interventions=interventions,
        ticks=ticks_out,
        patients=records,
        events=events,
        proposals=proposals,
    )


def _proposals_for(
    scenario: Scenario, h: Hospital, s: _State, t: int, forecast: float, saturation: float
) -> list[Proposal]:
    base = f"{scenario.id}:{h.id}:t{t}"
    out: list[Proposal] = []
    staff_to_fill_spaces = -(-h.treatment_spaces // h.patients_per_staff) - s.staff
    useful_staff = int(min(MAX_RECOMMENDED_STAFF, staff_to_fill_spaces))
    if useful_staff > 0:
        out.append(
            Proposal(
                id=f"{base}:staffing",
                kind="staffing",
                hospital_id=h.id,
                tick=t,
                forecast_saturation_pct=forecast,
                current_saturation_pct=saturation,
                recommended_staff=useful_staff,
                rationale=(
                    f"Projected saturation {forecast:.0f}% in 2 hours. Up to {useful_staff} "
                    f"more staff can be used before physical treatment spaces run out."
                ),
            )
        )
    out.append(
        Proposal(
            id=f"{base}:level_loading",
            kind="level_loading",
            hospital_id=h.id,
            tick=t,
            forecast_saturation_pct=forecast,
            current_saturation_pct=saturation,
            recommended_staff=None,
            rationale=(
                f"Projected saturation {forecast:.0f}% in 2 hours. Redirect least-urgent "
                f"inbound ambulances to nearby hospitals projected below 80%."
            ),
        )
    )
    return out
