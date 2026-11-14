"""Response schemas for the single-screen TriageX dashboard.

One `DashboardState` carries everything the command center renders: map, synchronized
comparison curves, coordination feed, proposal cards, and the hero impact metric. All
numbers are computed by the simulation; the frontend never derives deltas itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TrackName = Literal["baseline", "triagex"]
StatusName = Literal["normal", "pressure", "breach"]
Origin = Literal["simulation", "operator", "simulated_reply", "webhook"]
Severity = Literal["info", "warning", "critical", "success"]
CurvePhase = Literal["before_intervention", "staff_en_route", "shift_active", "after_shift"]


class ScenarioSummary(BaseModel):
    id: str
    name: str
    description: str
    focus_hospital_id: str
    surge_onset_tick: int


class RunInfo(BaseModel):
    run_id: str
    version: int = Field(description="Increments on every change to the run's state")
    scenario: ScenarioSummary
    seed: int
    created_at: datetime
    notice: str


class TimeWindow(BaseModel):
    start_tick: int
    end_tick: int  # exclusive
    start_clock: str
    end_clock: str


class Timeline(BaseModel):
    tick_minutes: int
    horizon_ticks: int
    clocks: list[str] = Field(description="Clock label for every tick in the 24-hour horizon")
    demo_window: TimeWindow
    suggested_ms_per_tick: int


class HospitalInfo(BaseModel):
    id: str
    name: str
    short_name: str
    lat: float
    lon: float
    treatment_spaces: int
    base_staff: int


class HospitalPoint(BaseModel):
    tick: int
    status: StatusName
    saturation_pct: float
    occupied: int
    queue_length: int
    ambulances_waiting: int
    staff_on_duty: int
    staffed_capacity: int
    mean_wait_minutes: float
    forecast_saturation_pct: float
    diverting: bool


class HospitalSeries(BaseModel):
    hospital_id: str
    baseline: list[HospitalPoint]
    triagex: list[HospitalPoint]


class CurvePoint(BaseModel):
    """One tick of the six-hour demo window, both futures side by side."""

    tick: int
    clock: str
    phase: CurvePhase
    diverged: bool
    baseline_focus_saturation_pct: float
    triagex_focus_saturation_pct: float
    baseline_patients_waiting: int
    triagex_patients_waiting: int
    baseline_ambulances_waiting: int
    triagex_ambulances_waiting: int
    baseline_ambulance_hours_cumulative: float
    triagex_ambulance_hours_cumulative: float
    ambulance_hours_saved_so_far: float


class Curves(BaseModel):
    focus_hospital_id: str
    window: TimeWindow
    points: list[CurvePoint]


class Band(BaseModel):
    kind: Literal["demo_window", "staff_lead_time", "shift_active", "level_loading_active"]
    start_tick: int
    end_tick: int
    label: str
    hospital_id: str | None = None


class Marker(BaseModel):
    tick: int
    clock: str
    kind: str
    label: str
    hospital_id: str | None = None
    in_demo_window: bool = True


class StatusTransition(BaseModel):
    tick: int
    clock: str
    track: TrackName
    hospital_id: str
    from_status: StatusName
    to_status: StatusName


class Flow(BaseModel):
    """Ambulances rerouted between hospitals in one tick (for map animation)."""

    tick: int
    track: TrackName
    from_hospital_id: str
    to_hospital_id: str
    kind: Literal["diversion", "level_loading"]
    count: int
    travel_ticks: int


class FeedEvent(BaseModel):
    id: str
    tick: int
    clock: str
    title: str
    detail: str
    kind: str
    track: Literal["both", "baseline", "triagex"]
    origin: Origin
    severity: Severity
    hospital_id: str | None = None
    wall_time: datetime | None = None
    in_demo_window: bool = True


class ProposalCard(BaseModel):
    id: str
    kind: Literal["staffing", "level_loading"]
    hospital_id: str
    tick: int
    clock: str
    title: str
    rationale: str
    forecast_saturation_pct: float
    current_saturation_pct: float
    recommended_staff: int | None
    status: Literal["available", "approved"]
    recommended: bool


class Confirmation(BaseModel):
    staff_id: str
    display_name: str
    source: Literal["simulated", "webhook"]
    received_at: datetime


class StaffingRequestView(BaseModel):
    request_id: str
    proposal_id: str
    hospital_id: str
    requested_staff: int
    confirmed_staff: int
    usable_staff: int
    status: Literal["awaiting_replies", "partially_confirmed", "fully_confirmed", "expired"]
    approved_tick: int
    effective_tick: int
    end_tick: int
    approved_at: datetime
    expires_at: datetime
    confirmations: list[Confirmation]


class HeroMetric(BaseModel):
    key: str
    label: str
    unit: str
    baseline: float
    triagex: float
    delta: float
    pct_change: float | None
    lower_is_better: bool
    improved: bool | None
    headline: str


class RecoveryView(BaseModel):
    hospital_id: str
    baseline_recovered_tick: int | None
    triagex_recovered_tick: int | None
    baseline_recovered_clock: str | None
    triagex_recovered_clock: str | None
    minutes_sooner: int | None
    baseline_breach_hours: float
    triagex_breach_hours: float
    headline: str


class Impact(BaseModel):
    state: Literal["awaiting_intervention", "intervention_scheduled"]
    primary: HeroMetric
    secondary: list[HeroMetric]
    recovery: RecoveryView
    window: TimeWindow


class Divergence(BaseModel):
    tick: int
    clock: str


class Meta(BaseModel):
    generated_at: datetime
    recompute_ms: float | None
    build_ms: float


class DashboardState(BaseModel):
    run: RunInfo
    timeline: Timeline
    hospitals: list[HospitalInfo]
    series: list[HospitalSeries]
    curves: Curves
    bands: list[Band]
    markers: list[Marker]
    transitions: list[StatusTransition]
    flows: list[Flow]
    feed: list[FeedEvent]
    proposals: list[ProposalCard]
    staffing_requests: list[StaffingRequestView]
    impact: Impact
    divergence: Divergence | None
    meta: Meta


class ForecastAccuracy(BaseModel):
    horizon_minutes: int
    samples: int
    bias_pct_points: float
    mean_absolute_error_pct_points: float


class Diagnostics(BaseModel):
    """Engineering detail for development and judge Q&A, kept off the main screen."""

    run_id: str
    version: int
    scenario_assumptions: list[str]
    model_limitations: list[str]
    forecast_accuracy: ForecastAccuracy
    all_proposals: list[ProposalCard]
    intervention_effects: list[dict]
    full_horizon_metrics: dict[str, dict]
    demo_window_metrics: dict[str, dict]
    track_digests: dict[str, str]
    recompute_history_ms: list[float]
