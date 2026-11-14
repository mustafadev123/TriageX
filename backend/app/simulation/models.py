"""Core data types for the TriageX surge simulation.

Every operational number here is synthetic. Hospital names and coordinates are used
only for geographic context.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

TICK_MINUTES = 15
TICKS_PER_HOUR = 60 // TICK_MINUTES
DEFAULT_HORIZON_TICKS = 24 * TICKS_PER_HOUR  # 24 simulated hours
DEMO_WINDOW_TICKS = 6 * TICKS_PER_HOUR  # 6-hour demo view

# Saturation thresholds (percent of physical treatment spaces).
PRESSURE_THRESHOLD_PCT = 85.0
BREACH_THRESHOLD_PCT = 100.0


class Mode(StrEnum):
    AMBULANCE = "ambulance"
    WALK_IN = "walk_in"


class Track(StrEnum):
    BASELINE = "baseline"
    TRIAGEX = "triagex"


class Status(StrEnum):
    NORMAL = "normal"
    PRESSURE = "pressure"
    BREACH = "breach"


class RerouteReason(StrEnum):
    DIVERSION = "diversion"
    LEVEL_LOADING = "level_loading"


class EventKind(StrEnum):
    DIVERSION_START = "diversion_start"
    DIVERSION_END = "diversion_end"
    AMBULANCE_DIVERTED = "ambulance_diverted"
    LEVEL_LOAD_REDIRECT = "level_load_redirect"
    STAFF_SHIFT_ACTIVE = "staff_shift_active"
    STAFF_SHIFT_ENDED = "staff_shift_ended"
    LEVEL_LOADING_ACTIVE = "level_loading_active"
    LEVEL_LOADING_ENDED = "level_loading_ended"


def classify_saturation(saturation_pct: float) -> Status:
    if saturation_pct >= BREACH_THRESHOLD_PCT:
        return Status.BREACH
    if saturation_pct >= PRESSURE_THRESHOLD_PCT:
        return Status.PRESSURE
    return Status.NORMAL


@dataclass(frozen=True, slots=True)
class Hospital:
    """Synthetic emergency department configuration."""

    id: str
    name: str
    short_name: str
    lat: float
    lon: float
    treatment_spaces: int  # physical ED treatment spaces
    base_staff: int  # synthetic nurse-equivalent staff on duty
    patients_per_staff: float
    base_arrivals_per_hour: float  # before the diurnal profile and scenario multipliers
    ambulance_fraction: float
    expected_service_minutes: float  # forecaster's prior for mean length of stay

    def staffed_capacity(self, staff: int) -> int:
        """Patients that can be in treatment at once: limited by staff AND physical space."""
        return min(self.treatment_spaces, math.floor(max(staff, 0) * self.patients_per_staff))


@dataclass(frozen=True, slots=True)
class Patient:
    """One pre-sampled synthetic arrival. Shared, unchanged, by both tracks."""

    seq: int  # global, stable tie-breaker
    id: str
    hospital_id: str  # intended destination
    arrival_tick: int
    mode: Mode
    acuity: int  # 1 = most urgent, 3 = least urgent
    service_ticks: int
    source: str  # "baseline" or "surge"


@dataclass(slots=True)
class PatientRecord:
    """What happened to one patient in one track."""

    seq: int
    patient_id: str
    mode: Mode
    acuity: int
    intended_hospital_id: str
    original_arrival_tick: int
    service_ticks: int
    final_hospital_id: str | None = None
    ed_arrival_tick: int | None = None
    placement_tick: int | None = None
    reroute_reason: RerouteReason | None = None
    extra_travel_ticks: int = 0
    intervention_id: str | None = None


@dataclass(slots=True)
class HospitalTick:
    """State of one hospital at the end of one tick."""

    tick: int
    hospital_id: str
    staff_on_duty: int
    staffed_capacity: int
    treatment_spaces: int
    occupied: int
    queue_length: int
    ambulance_queue_length: int
    arrivals: int
    placements: int
    completions: int
    saturation_pct: float
    status: Status
    diverting: bool
    diverted_out: int
    diverted_in: int
    transfers_out: int
    transfers_in: int
    mean_current_wait_minutes: float
    staff_utilization_pct: float
    forecast_saturation_pct: float


@dataclass(frozen=True, slots=True)
class SimEvent:
    tick: int
    kind: EventKind
    hospital_id: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Proposal:
    """A suggested intervention. Never applied without explicit human approval."""

    id: str
    kind: str  # "staffing" or "level_loading"
    hospital_id: str
    tick: int
    forecast_saturation_pct: float
    current_saturation_pct: float
    recommended_staff: int | None
    rationale: str


@dataclass(slots=True)
class TrackResult:
    track: Track
    scenario_id: str
    horizon_ticks: int
    start_tick: int  # negative when a warm-up period was simulated
    hospital_ids: tuple[str, ...]
    interventions: tuple
    ticks: list[list[HospitalTick]]  # ticks[t][hospital_index], t in [0, horizon)
    patients: list[PatientRecord]
    events: list[SimEvent]
    proposals: list[Proposal]

    def hospital_series(self, hospital_id: str) -> list[HospitalTick]:
        i = self.hospital_ids.index(hospital_id)
        return [row[i] for row in self.ticks]
