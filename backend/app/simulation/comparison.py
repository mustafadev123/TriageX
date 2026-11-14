"""One surge, two futures: run Track A (baseline) and Track B (TriageX) on identical inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass

from app.simulation.arrivals import generate_patients
from app.simulation.engine import simulate
from app.simulation.hospitals import DEFAULT_NETWORK, Network
from app.simulation.interventions import (
    Intervention,
    LevelLoadingIntervention,
    StaffingIntervention,
)
from app.simulation.metrics import METRIC_NAMES, Metrics, summarize
from app.simulation.models import EventKind, Patient, Track, TrackResult
from app.simulation.scenarios import Scenario, get_scenario


@dataclass(frozen=True, slots=True)
class MetricDelta:
    baseline: float
    triagex: float
    delta: float  # triagex - baseline
    pct_change: float | None  # None when the baseline value is zero


@dataclass(frozen=True, slots=True)
class TimelineMarker:
    tick: int
    clock: str
    kind: str
    label: str
    hospital_id: str | None = None
    intervention_id: str | None = None


@dataclass(frozen=True, slots=True)
class InterventionEffect:
    intervention_id: str
    kind: str
    hospital_id: str
    approved_tick: int
    effective_tick: int
    end_tick: int
    confirmed_staff: int | None
    staffed_capacity_before: int | None
    staffed_capacity_with_staff: int | None
    usable_staff: int | None  # confirmations that add capacity, given physical space
    unusable_staff: int | None  # confirmations blocked by the physical space limit
    redirects_executed: int | None
    hospital_deltas: dict[str, MetricDelta]


@dataclass(slots=True)
class Comparison:
    scenario: Scenario
    seed: int
    patients: tuple[Patient, ...]
    baseline: TrackResult
    triagex: TrackResult
    baseline_metrics: Metrics
    triagex_metrics: Metrics
    deltas: dict[str, MetricDelta]
    window_baseline_metrics: Metrics
    window_triagex_metrics: Metrics
    window_deltas: dict[str, MetricDelta]
    first_divergence_tick: int | None
    markers: list[TimelineMarker]
    intervention_effects: list[InterventionEffect]


def metric_deltas(a: Metrics, b: Metrics) -> dict[str, MetricDelta]:
    out: dict[str, MetricDelta] = {}
    for name in METRIC_NAMES:
        va, vb = getattr(a, name), getattr(b, name)
        if isinstance(va, str):
            continue
        pct = round((vb - va) / va * 100, 1) if va else None
        out[name] = MetricDelta(float(va), float(vb), round(vb - va, 2), pct)
    return out


def first_divergence_tick(a: TrackResult, b: TrackResult) -> int | None:
    for t, (row_a, row_b) in enumerate(zip(a.ticks, b.ticks, strict=True)):
        if row_a != row_b:
            return t
    return None


def _usable_staff(hospital, confirmed: int) -> int:
    target = hospital.staffed_capacity(hospital.base_staff + confirmed)
    for k in range(confirmed + 1):
        if hospital.staffed_capacity(hospital.base_staff + k) == target:
            return k
    return confirmed


def _effects(
    network: Network, baseline: TrackResult, triagex: TrackResult
) -> list[InterventionEffect]:
    effects: list[InterventionEffect] = []
    for iv in triagex.interventions:
        h = network.get(iv.hospital_id)
        deltas = metric_deltas(summarize(baseline, h.id), summarize(triagex, h.id))
        if isinstance(iv, StaffingIntervention):
            usable = _usable_staff(h, iv.confirmed_staff)
            effects.append(
                InterventionEffect(
                    iv.id,
                    "staffing",
                    h.id,
                    iv.approved_tick,
                    iv.effective_tick,
                    iv.end_tick,
                    iv.confirmed_staff,
                    h.staffed_capacity(h.base_staff),
                    h.staffed_capacity(h.base_staff + iv.confirmed_staff),
                    usable,
                    iv.confirmed_staff - usable,
                    None,
                    deltas,
                )
            )
        elif isinstance(iv, LevelLoadingIntervention):
            redirects = sum(1 for r in triagex.patients if r.intervention_id == iv.id)
            effects.append(
                InterventionEffect(
                    iv.id,
                    "level_loading",
                    h.id,
                    iv.approved_tick,
                    iv.effective_tick,
                    iv.end_tick,
                    None,
                    None,
                    None,
                    None,
                    None,
                    redirects,
                    deltas,
                )
            )
    return effects


def _markers(
    scenario: Scenario, baseline: TrackResult, triagex: TrackResult, divergence: int | None
) -> list[TimelineMarker]:
    c = scenario.clock_label
    start, end = scenario.demo_window
    m = [
        TimelineMarker(
            scenario.surge_onset_tick,
            c(scenario.surge_onset_tick),
            "surge_onset",
            f"{scenario.name} begins",
        ),
        TimelineMarker(start, c(start), "demo_window_start", "Demo window starts"),
        TimelineMarker(end, c(end), "demo_window_end", "Demo window ends"),
    ]
    for p in baseline.proposals:
        m.append(
            TimelineMarker(
                p.tick,
                c(p.tick),
                "proposal",
                f"TriageX proposes {p.kind.replace('_', '-')}",
                p.hospital_id,
                p.id,
            )
        )
    for iv in triagex.interventions:
        m.append(
            TimelineMarker(
                iv.approved_tick,
                c(iv.approved_tick),
                "approval",
                "Operator approves intervention",
                iv.hospital_id,
                iv.id,
            )
        )
        if isinstance(iv, StaffingIntervention):
            if iv.confirmed_staff > 0:
                m.append(
                    TimelineMarker(
                        iv.effective_tick,
                        c(iv.effective_tick),
                        "shift_active",
                        f"{iv.confirmed_staff} confirmed staff on shift",
                        iv.hospital_id,
                        iv.id,
                    )
                )
                m.append(
                    TimelineMarker(
                        iv.end_tick,
                        c(iv.end_tick),
                        "shift_end",
                        "Surge shift ends",
                        iv.hospital_id,
                        iv.id,
                    )
                )
        else:
            m.append(
                TimelineMarker(
                    iv.effective_tick,
                    c(iv.effective_tick),
                    "level_loading_active",
                    "Level-loading active",
                    iv.hospital_id,
                    iv.id,
                )
            )
            m.append(
                TimelineMarker(
                    iv.end_tick,
                    c(iv.end_tick),
                    "level_loading_end",
                    "Level-loading ends",
                    iv.hospital_id,
                    iv.id,
                )
            )
    if divergence is not None:
        m.append(TimelineMarker(divergence, c(divergence), "divergence", "Tracks diverge"))
    for e in baseline.events:
        if e.kind is EventKind.DIVERSION_START:
            m.append(
                TimelineMarker(
                    e.tick,
                    c(e.tick),
                    "baseline_diversion",
                    "Baseline: diversion starts",
                    e.hospital_id,
                )
            )
    return sorted(m, key=lambda x: (x.tick, x.kind, x.hospital_id or "", x.intervention_id or ""))


def run_comparison(
    scenario: Scenario | str,
    seed: int,
    interventions: tuple[Intervention, ...] = (),
    network: Network = DEFAULT_NETWORK,
) -> Comparison:
    scenario = get_scenario(scenario) if isinstance(scenario, str) else scenario
    patients = generate_patients(network, scenario, seed)
    baseline = simulate(network, scenario, patients, (), Track.BASELINE)
    triagex = simulate(network, scenario, patients, interventions, Track.TRIAGEX)
    return build_comparison(scenario, seed, patients, baseline, triagex, network)


def build_comparison(
    scenario: Scenario,
    seed: int,
    patients: tuple[Patient, ...],
    baseline: TrackResult,
    triagex: TrackResult,
    network: Network = DEFAULT_NETWORK,
    baseline_metrics: tuple[Metrics, Metrics] | None = None,
) -> Comparison:
    """Assemble a comparison from already-simulated tracks.

    `baseline_metrics` lets callers reuse the (immutable) baseline's full-horizon and
    demo-window metrics when only Track B was re-simulated.
    """
    window = scenario.demo_window
    if baseline_metrics is None:
        baseline_metrics = (summarize(baseline), summarize(baseline, tick_range=window))
    bm, wbm = baseline_metrics
    tm, wtm = summarize(triagex), summarize(triagex, tick_range=window)
    divergence = first_divergence_tick(baseline, triagex)
    return Comparison(
        scenario=scenario,
        seed=seed,
        patients=patients,
        baseline=baseline,
        triagex=triagex,
        baseline_metrics=bm,
        triagex_metrics=tm,
        deltas=metric_deltas(bm, tm),
        window_baseline_metrics=wbm,
        window_triagex_metrics=wtm,
        window_deltas=metric_deltas(wbm, wtm),
        first_divergence_tick=divergence,
        markers=_markers(scenario, baseline, triagex, divergence),
        intervention_effects=_effects(network, baseline, triagex),
    )


def _jsonable(obj: object) -> object:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def track_digest(result: TrackResult) -> str:
    """SHA-256 over everything a track produced except its track label."""
    payload = {
        "ticks": _jsonable(result.ticks),
        "patients": _jsonable(result.patients),
        "events": _jsonable(result.events),
        "proposals": _jsonable(result.proposals),
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
