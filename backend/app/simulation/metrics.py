"""Metrics computed only from a TrackResult's tick states, patient ledger, and events.

Wait times are measured from ED arrival to placement in a treatment space, at 15-minute
resolution. Patients still waiting at the end of the horizon contribute a censored wait
(horizon end minus ED arrival), so long queues are never hidden by being unfinished.
Patients from the warm-up period (ED arrival before tick 0) are excluded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

from app.simulation.models import TICK_MINUTES, EventKind, Mode, Status, TrackResult


@dataclass(frozen=True, slots=True)
class Metrics:
    patients_arrived: int
    patients_placed: int
    patients_waiting_at_end: int
    mean_wait_minutes: float
    p90_wait_minutes: float
    max_wait_minutes: float
    peak_saturation_pct: float
    peak_saturation_tick: int
    peak_saturation_hospital_id: str
    hospital_ticks_in_breach: int
    total_queue_patient_hours: float
    ambulance_patients: int
    mean_handoff_minutes: float
    ambulance_handoff_hours: float
    ambulance_reroute_travel_hours: float
    ambulance_hours_lost: float
    diversion_onsets: int
    diverted_ambulances: int
    transfers: int
    mean_staff_utilization_pct: float


# Metrics where a lower value is better (used for labelling deltas, never for scoring).
LOWER_IS_BETTER = {
    "mean_wait_minutes", "p90_wait_minutes", "max_wait_minutes", "peak_saturation_pct",
    "hospital_ticks_in_breach", "total_queue_patient_hours", "mean_handoff_minutes",
    "ambulance_handoff_hours", "ambulance_reroute_travel_hours", "ambulance_hours_lost",
    "diversion_onsets", "diverted_ambulances", "patients_waiting_at_end",
}  # fmt: skip

METRIC_NAMES = tuple(f.name for f in fields(Metrics))


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(
    result: TrackResult,
    hospital_id: str | None = None,
    tick_range: tuple[int, int] | None = None,
) -> Metrics:
    """Summarize a track for the whole network (or one hospital) over a tick range.

    Patient metrics cover patients whose ED arrival falls inside the range. Tick metrics
    cover ticks inside the range.
    """
    horizon = result.horizon_ticks
    start, end = tick_range or (0, horizon)
    start, end = max(0, start), min(horizon, end)
    if hospital_id is not None and hospital_id not in result.hospital_ids:
        raise ValueError(f"Unknown hospital '{hospital_id}'")

    patients = [
        r
        for r in result.patients
        if r.ed_arrival_tick is not None
        and start <= r.ed_arrival_tick < end
        and (hospital_id is None or r.final_hospital_id == hospital_id)
    ]
    waits = sorted(
        float(
            ((r.placement_tick if r.placement_tick is not None else horizon) - r.ed_arrival_tick)
            * TICK_MINUTES
        )
        for r in patients
    )
    ambulance = [r for r in patients if r.mode is Mode.AMBULANCE]
    handoffs = [
        ((r.placement_tick if r.placement_tick is not None else horizon) - r.ed_arrival_tick)
        * TICK_MINUTES
        for r in ambulance
    ]
    reroute_minutes = sum(r.extra_travel_ticks * TICK_MINUTES for r in ambulance)

    rows = [
        row
        for t in range(start, end)
        for row in result.ticks[t]
        if hospital_id is None or row.hospital_id == hospital_id
    ]
    peak = max(rows, key=lambda r: (r.saturation_pct, -r.tick), default=None)
    handoff_hours = sum(handoffs) / 60
    travel_hours = reroute_minutes / 60

    onsets = sum(
        1
        for e in result.events
        if e.kind is EventKind.DIVERSION_START
        and start <= e.tick < end
        and (hospital_id is None or e.hospital_id == hospital_id)
    )

    return Metrics(
        patients_arrived=len(patients),
        patients_placed=sum(1 for r in patients if r.placement_tick is not None),
        patients_waiting_at_end=sum(1 for r in patients if r.placement_tick is None),
        mean_wait_minutes=round(_mean(waits), 2),
        p90_wait_minutes=_percentile(waits, 90),
        max_wait_minutes=waits[-1] if waits else 0.0,
        peak_saturation_pct=peak.saturation_pct if peak else 0.0,
        peak_saturation_tick=peak.tick if peak else start,
        peak_saturation_hospital_id=peak.hospital_id if peak else "",
        hospital_ticks_in_breach=sum(1 for r in rows if r.status is Status.BREACH),
        total_queue_patient_hours=round(sum(r.queue_length for r in rows) * TICK_MINUTES / 60, 2),
        ambulance_patients=len(ambulance),
        mean_handoff_minutes=round(_mean([float(h) for h in handoffs]), 2),
        ambulance_handoff_hours=round(handoff_hours, 2),
        ambulance_reroute_travel_hours=round(travel_hours, 2),
        ambulance_hours_lost=round(handoff_hours + travel_hours, 2),
        diversion_onsets=onsets,
        diverted_ambulances=sum(r.diverted_out for r in rows),
        transfers=sum(r.transfers_out for r in rows),
        mean_staff_utilization_pct=round(_mean([r.staff_utilization_pct for r in rows]), 2),
    )


RECOVERY_CALM_TICKS = 4  # one simulated hour below the pressure threshold


@dataclass(frozen=True, slots=True)
class Recovery:
    """How a hospital came back from a capacity breach."""

    first_breach_tick: int | None  # None: the hospital never breached in the range
    recovered_tick: int | None  # start of the first calm hour after the first breach
    breach_ticks: int


def ambulance_waiting_hours_by_tick(
    result: TrackResult, tick_range: tuple[int, int] | None = None
) -> list[float]:
    """Cumulative ambulance-hours spent waiting for handoff, network-wide, tick by tick.

    Derived from tick state: every ambulance still waiting for a treatment space at the
    end of a tick adds one tick (15 minutes). Over a range that covers every waiting
    ambulance, the final value equals the ledger-based handoff hours.
    """
    start, end = tick_range or (0, result.horizon_ticks)
    total, out = 0, []
    for t in range(start, end):
        total += sum(row.ambulance_queue_length for row in result.ticks[t])
        out.append(round(total * TICK_MINUTES / 60, 2))
    return out


def hospital_recovery(
    result: TrackResult, hospital_id: str, tick_range: tuple[int, int] | None = None
) -> Recovery:
    start, end = tick_range or (0, result.horizon_ticks)
    series = result.hospital_series(hospital_id)[start:end]
    first_breach = next((r.tick for r in series if r.status is Status.BREACH), None)
    breach_ticks = sum(1 for r in series if r.status is Status.BREACH)
    if first_breach is None:
        return Recovery(None, None, 0)
    calm_run, recovered = 0, None
    for r in series:
        if r.tick <= first_breach:
            continue
        calm_run = calm_run + 1 if r.status is Status.NORMAL else 0
        if calm_run == RECOVERY_CALM_TICKS:
            recovered = r.tick - RECOVERY_CALM_TICKS + 1
            break
    return Recovery(first_breach, recovered, breach_ticks)
