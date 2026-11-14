"""Two-hour-ahead saturation projection.

The forecaster sees only an `ObservedState` snapshot built from information available at
the current simulated tick: current occupancy, queue, staffed capacity, the arrivals
already observed in the trailing hour, the ordinary diurnal shape, and a configured prior
for length of stay. It never sees the scenario definition or future arrivals.

It is a simple fluid projection, not a validated clinical forecast.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.simulation.arrivals import DIURNAL_PROFILE
from app.simulation.models import TICK_MINUTES, TICKS_PER_HOUR

FORECAST_HORIZON_TICKS = 2 * TICKS_PER_HOUR
TRAILING_TICKS = TICKS_PER_HOUR
PROPOSAL_THRESHOLD_PCT = 90.0
PROPOSAL_REARM_PCT = 80.0


@dataclass(frozen=True, slots=True)
class ObservedState:
    clock_minutes: int
    occupied: int
    queue_length: int
    staffed_capacity: int
    treatment_spaces: int
    recent_arrivals: tuple[int, ...]  # ED arrivals in the trailing ticks, oldest first
    expected_service_minutes: float


def _hour(clock_minutes: int) -> int:
    return (clock_minutes // 60) % 24


def project_saturation(state: ObservedState, horizon_ticks: int = FORECAST_HORIZON_TICKS) -> float:
    """Projected saturation (% of treatment spaces) `horizon_ticks` from now."""
    recent = state.recent_arrivals[-TRAILING_TICKS:]
    trailing_rate = sum(recent) / len(recent) if recent else 0.0
    # Shape the observed rate by the ordinary diurnal profile, relative to the trailing hour.
    span_minutes = len(recent) * TICK_MINUTES if recent else TICK_MINUTES
    ref = DIURNAL_PROFILE[_hour(state.clock_minutes - span_minutes // 2)]

    mean_service_ticks = max(1.0, state.expected_service_minutes / TICK_MINUTES)
    occupied = float(state.occupied)
    queue = float(state.queue_length)
    for k in range(1, horizon_ticks + 1):
        shape = DIURNAL_PROFILE[_hour(state.clock_minutes + k * TICK_MINUTES)] / ref
        queue += trailing_rate * shape
        occupied -= occupied / mean_service_ticks
        placed = min(queue, max(0.0, state.staffed_capacity - occupied))
        occupied += placed
        queue -= placed
    return round((occupied + queue) / state.treatment_spaces * 100, 2)
