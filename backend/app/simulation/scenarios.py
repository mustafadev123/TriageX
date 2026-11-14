"""The four synthetic surge scenarios.

Each scenario is a set of transparent demand assumptions: time windows that multiply
the ordinary arrival rate, plus discrete surge bursts (for example, crash victims).
Nothing here encodes an outcome; outcomes come only from running the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.simulation.models import DEFAULT_HORIZON_TICKS, DEMO_WINDOW_TICKS, TICK_MINUTES

AcuityMix = tuple[float, float, float]  # P(acuity 1), P(acuity 2), P(acuity 3)


@dataclass(frozen=True, slots=True)
class DemandWindow:
    """Multiplies ordinary arrivals by up to `peak_multiplier`, with linear ramps."""

    start_tick: int
    end_tick: int  # exclusive
    peak_multiplier: float
    hospital_ids: tuple[str, ...] | None = None  # None = every hospital
    ramp_ticks: int = 2

    def factor(self, hospital_id: str, tick: int) -> float:
        if self.hospital_ids is not None and hospital_id not in self.hospital_ids:
            return 1.0
        if not (self.start_tick <= tick < self.end_tick):
            return 1.0
        weight = 1.0
        if self.ramp_ticks > 0:
            rise = (tick - self.start_tick + 1) / self.ramp_ticks
            fall = (self.end_tick - tick) / self.ramp_ticks
            weight = min(1.0, rise, fall)
        return 1.0 + (self.peak_multiplier - 1.0) * weight


@dataclass(frozen=True, slots=True)
class SurgeBurst:
    """Extra patients bound for one hospital, front-loaded over `duration_ticks`."""

    hospital_id: str
    start_tick: int
    duration_ticks: int
    expected_patients: float
    ambulance_fraction: float
    acuity_mix: AcuityMix

    def expected_at(self, tick: int) -> float:
        k = tick - self.start_tick
        if not (0 <= k < self.duration_ticks):
            return 0.0
        total_weight = self.duration_ticks * (self.duration_ticks + 1) / 2
        return self.expected_patients * (self.duration_ticks - k) / total_weight


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    name: str
    description: str
    start_clock_minutes: int  # wall-clock time of tick 0 (minutes after midnight)
    surge_onset_tick: int
    demo_window_start_tick: int
    demand_windows: tuple[DemandWindow, ...] = ()
    bursts: tuple[SurgeBurst, ...] = ()
    service_multiplier: float = 1.0
    walk_in_acuity_mix: AcuityMix | None = None
    ambulance_fraction_delta: float = 0.0
    horizon_ticks: int = DEFAULT_HORIZON_TICKS
    demo_window_ticks: int = DEMO_WINDOW_TICKS
    assumptions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def demo_window(self) -> tuple[int, int]:
        return self.demo_window_start_tick, self.demo_window_start_tick + self.demo_window_ticks

    def demand_multiplier(self, hospital_id: str, tick: int) -> float:
        m = 1.0
        for w in self.demand_windows:
            m *= w.factor(hospital_id, tick)
        return m

    def clock_minutes(self, tick: int) -> int:
        return self.start_clock_minutes + tick * TICK_MINUTES

    def clock_hour(self, tick: int) -> int:
        return (self.clock_minutes(tick) // 60) % 24

    def clock_label(self, tick: int) -> str:
        total = self.clock_minutes(tick)
        day, minutes = divmod(total, 24 * 60)
        label = f"{minutes // 60:02d}:{minutes % 60:02d}"
        return label if day == 0 else f"{label} (+{day}d)"


ALL_ATL = ("grady", "emory_uh", "emory_midtown", "piedmont", "kennestone")

I85_PILEUP = Scenario(
    id="i85_pileup",
    name="I-85 Multi-Vehicle Pileup",
    description=(
        "A synthetic multi-vehicle crash on I-85 near the Downtown Connector at 16:00 sends a "
        "front-loaded wave of high-acuity ambulance patients, mostly to Grady, during the "
        "evening rush."
    ),
    start_clock_minutes=12 * 60,  # tick 0 = 12:00
    surge_onset_tick=16,  # 16:00
    demo_window_start_tick=12,  # 15:00-21:00
    bursts=(
        SurgeBurst("grady", 16, 8, 42.0, 0.85, (0.30, 0.45, 0.25)),
        SurgeBurst("emory_midtown", 16, 8, 14.0, 0.80, (0.20, 0.50, 0.30)),
        SurgeBurst("piedmont", 17, 8, 7.0, 0.70, (0.15, 0.50, 0.35)),
        SurgeBurst("emory_uh", 17, 8, 4.0, 0.70, (0.15, 0.50, 0.35)),
    ),
    demand_windows=(
        # Rush-hour traffic injuries elsewhere on the corridor.
        DemandWindow(16, 28, 1.15, ("grady", "emory_midtown", "piedmont"), ramp_ticks=2),
    ),
    assumptions=(
        "67 expected surge patients across four hospitals, 63% of them bound for Grady.",
        "Surge arrivals are front-loaded over 2 hours (linearly decreasing).",
        "Surge patients skew high-acuity; most arrive by ambulance.",
    ),
)

BIG_GAME = Scenario(
    id="big_game",
    name="Big Game Downtown",
    description=(
        "A synthetic late-evening surge after a downtown stadium event: intoxication, minor "
        "trauma, and crowd injuries, concentrated at downtown and midtown EDs."
    ),
    start_clock_minutes=12 * 60,  # tick 0 = 12:00
    surge_onset_tick=40,  # 22:00
    demo_window_start_tick=36,  # 21:00-03:00
    demand_windows=(
        DemandWindow(40, 56, 2.5, ("grady",), ramp_ticks=3),
        DemandWindow(40, 56, 2.2, ("emory_midtown",), ramp_ticks=3),
        DemandWindow(40, 56, 1.3, ("piedmont",), ramp_ticks=3),
    ),
    bursts=(
        SurgeBurst("grady", 41, 6, 28.0, 0.40, (0.15, 0.45, 0.40)),
        SurgeBurst("emory_midtown", 41, 6, 16.0, 0.35, (0.10, 0.45, 0.45)),
    ),
    walk_in_acuity_mix=(0.04, 0.36, 0.60),
    assumptions=(
        "Post-event demand at Grady rises up to 2.5x ordinary (Midtown 2.2x) from 22:00 to 02:00.",
        "44 expected extra crowd-injury patients at Grady and Midtown just after the event.",
        "Surge patients skew lower-acuity and arrive mainly on foot.",
    ),
)

HEAT_WAVE = Scenario(
    id="heat_wave",
    name="Summer Heat Wave",
    description=(
        "A synthetic afternoon heat-illness surge across the whole metro area, with more "
        "ambulance arrivals and slightly longer treatment times."
    ),
    start_clock_minutes=8 * 60,  # tick 0 = 08:00
    surge_onset_tick=20,  # 13:00
    demo_window_start_tick=20,  # 13:00-19:00
    demand_windows=(DemandWindow(20, 48, 1.4, None, ramp_ticks=6),),
    service_multiplier=1.1,
    ambulance_fraction_delta=0.05,
    assumptions=(
        "Metro-wide demand rises up to 1.4x ordinary from 13:00 to 20:00.",
        "Treatment times are 10% longer; the ambulance share rises 5 points.",
    ),
)

RESPIRATORY_WAVE = Scenario(
    id="respiratory_wave",
    name="Respiratory Illness Wave",
    description=(
        "A synthetic multi-day respiratory illness wave: demand is elevated all day across "
        "the metro area, with an evening peak and longer treatment times."
    ),
    start_clock_minutes=6 * 60,  # tick 0 = 06:00
    surge_onset_tick=0,  # already under way
    demo_window_start_tick=40,  # 16:00-22:00
    demand_windows=(
        DemandWindow(-10_000, 10_000, 1.15, None, ramp_ticks=0),
        DemandWindow(40, 64, 1.15, None, ramp_ticks=4),
    ),
    service_multiplier=1.1,
    ambulance_fraction_delta=-0.04,
    walk_in_acuity_mix=(0.05, 0.50, 0.45),
    assumptions=(
        "Demand is 1.15x ordinary all day, rising to about 1.32x from 16:00 to 22:00.",
        "Treatment times are 10% longer; more patients arrive on foot.",
        "The warm-up period also runs at elevated demand because the wave is multi-day.",
    ),
)

SCENARIOS: dict[str, Scenario] = {
    s.id: s for s in (I85_PILEUP, BIG_GAME, HEAT_WAVE, RESPIRATORY_WAVE)
}


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise ValueError(
            f"Unknown scenario '{scenario_id}'. Options: {', '.join(SCENARIOS)}"
        ) from None
