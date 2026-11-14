"""Builds the single-screen DashboardState (and Diagnostics) from a demo Run."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime

from app.config import SYNTHETIC_DATA_NOTICE
from app.models.dashboard import (
    Band,
    Confirmation,
    CurvePoint,
    Curves,
    DashboardState,
    Diagnostics,
    Divergence,
    FeedEvent,
    Flow,
    ForecastAccuracy,
    HeroMetric,
    HospitalInfo,
    HospitalPoint,
    HospitalSeries,
    Impact,
    Marker,
    Meta,
    ProposalCard,
    RecoveryView,
    RunInfo,
    ScenarioSummary,
    StaffingRequestView,
    StatusTransition,
    Timeline,
    TimeWindow,
)
from app.services.demo_service import DemoService, Run
from app.simulation.comparison import MetricDelta, track_digest
from app.simulation.forecast import FORECAST_HORIZON_TICKS
from app.simulation.interventions import STAFF_LEAD_TICKS
from app.simulation.metrics import (
    ambulance_waiting_hours_by_tick,
    hospital_recovery,
)
from app.simulation.models import TICK_MINUTES, EventKind, HospitalTick, RerouteReason, TrackResult

SUGGESTED_MS_PER_TICK = 350  # 24 ticks = about 8.4 seconds of playback

MODEL_LIMITATIONS = (
    "Synthetic model; not clinically validated and not based on real hospital data.",
    "15-minute resolution; one pooled staff number per hospital; no inpatient boarding, "
    "lab or imaging delays, or patients leaving without being seen.",
    "Forecast is a simple fluid projection from the trailing hour; it overshoots during "
    "sudden bursts and can raise false alarms.",
    "Level-loading assumes immediate EMS compliance.",
    "Results are exactly reproducible on one platform and Python version.",
)


def _window(run: Run) -> TimeWindow:
    start, end = run.scenario.demo_window
    c = run.scenario.clock_label
    return TimeWindow(start_tick=start, end_tick=end, start_clock=c(start), end_clock=c(end))


def _point(row: HospitalTick) -> HospitalPoint:
    return HospitalPoint(
        tick=row.tick,
        status=row.status.value,
        saturation_pct=row.saturation_pct,
        occupied=row.occupied,
        queue_length=row.queue_length,
        ambulances_waiting=row.ambulance_queue_length,
        staff_on_duty=row.staff_on_duty,
        staffed_capacity=row.staffed_capacity,
        mean_wait_minutes=row.mean_current_wait_minutes,
        forecast_saturation_pct=row.forecast_saturation_pct,
        diverting=row.diverting,
    )


def _hero(
    key: str,
    label: str,
    unit: str,
    d: MetricDelta,
    phrases: tuple[str, str],
    decimals: int = 1,
    intervention_scheduled: bool = False,
) -> HeroMetric:
    """`phrases` = (when lower, when higher), e.g. ("fewer ambulance hours lost", ...)."""
    improved = None if d.delta == 0 else d.delta < 0
    if d.delta == 0:
        headline = "No change" if intervention_scheduled else "No difference yet"
    else:
        pct = f" ({d.pct_change:+.0f}%)" if d.pct_change is not None else ""
        headline = f"{abs(d.delta):.{decimals}f} {phrases[0] if improved else phrases[1]}{pct}"
    return HeroMetric(
        key=key, label=label, unit=unit, baseline=d.baseline, triagex=d.triagex,
        delta=d.delta, pct_change=d.pct_change, lower_is_better=True, improved=improved,
        headline=headline,
    )  # fmt: skip


def _delta(a: float, b: float) -> MetricDelta:
    pct = round((b - a) / a * 100, 1) if a else None
    return MetricDelta(float(a), float(b), round(b - a, 2), pct)


def _active_staffing(run: Run):
    confirmed = [r for r in run.staffing_requests.values() if r.confirmed_staff > 0]
    return min(confirmed, key=lambda r: r.approved_tick, default=None)


def _phase(run: Run, tick: int) -> str:
    request = _active_staffing(run)
    if request is None:
        return "before_intervention"
    iv = request.intervention()
    if tick < iv.approved_tick:
        return "before_intervention"
    if tick < iv.effective_tick:
        return "staff_en_route"
    if tick < iv.end_tick:
        return "shift_active"
    return "after_shift"


def _curves(run: Run, divergence: int | None) -> Curves:
    c = run.comparison
    window = _window(run)
    rng = (window.start_tick, window.end_tick)
    focus = run.scenario.focus_hospital_id
    a_cum = ambulance_waiting_hours_by_tick(c.baseline, rng)
    b_cum = ambulance_waiting_hours_by_tick(c.triagex, rng)
    fa, fb = c.baseline.hospital_series(focus), c.triagex.hospital_series(focus)
    points = []
    for k, t in enumerate(range(*rng)):
        ra, rb = c.baseline.ticks[t], c.triagex.ticks[t]
        points.append(
            CurvePoint(
                tick=t,
                clock=run.scenario.clock_label(t),
                phase=_phase(run, t),
                diverged=divergence is not None and t >= divergence,
                baseline_focus_saturation_pct=fa[t].saturation_pct,
                triagex_focus_saturation_pct=fb[t].saturation_pct,
                baseline_patients_waiting=sum(r.queue_length for r in ra),
                triagex_patients_waiting=sum(r.queue_length for r in rb),
                baseline_ambulances_waiting=sum(r.ambulance_queue_length for r in ra),
                triagex_ambulances_waiting=sum(r.ambulance_queue_length for r in rb),
                baseline_ambulance_hours_cumulative=a_cum[k],
                triagex_ambulance_hours_cumulative=b_cum[k],
                ambulance_hours_saved_so_far=round(a_cum[k] - b_cum[k], 2),
            )
        )
    return Curves(focus_hospital_id=focus, window=window, points=points)


def _recovery(run: Run) -> RecoveryView:
    c, sc = run.comparison, run.scenario
    focus = sc.focus_hospital_id
    rng = (sc.surge_onset_tick, sc.horizon_ticks)
    a, b = hospital_recovery(c.baseline, focus, rng), hospital_recovery(c.triagex, focus, rng)
    name = run.network.get(focus).short_name
    sooner = None
    if a.recovered_tick is not None and b.recovered_tick is not None:
        sooner = (a.recovered_tick - b.recovered_tick) * TICK_MINUTES
    if a.first_breach_tick is None:
        headline = f"{name} stayed below capacity"
    elif sooner is None and b.recovered_tick is not None:
        headline = f"{name} back to normal by {sc.clock_label(b.recovered_tick)}; "
        headline += "without TriageX it had not recovered"
    elif sooner is None:
        headline = f"{name} had not recovered by the end of the simulation"
    elif sooner > 0:
        headline = f"{name} back to normal {sooner} min sooner"
    elif sooner < 0:
        headline = f"{name} back to normal {-sooner} min later"
    else:
        headline = f"{name} recovery time unchanged"
    label = sc.clock_label
    return RecoveryView(
        hospital_id=focus,
        baseline_recovered_tick=a.recovered_tick,
        triagex_recovered_tick=b.recovered_tick,
        baseline_recovered_clock=label(a.recovered_tick) if a.recovered_tick is not None else None,
        triagex_recovered_clock=label(b.recovered_tick) if b.recovered_tick is not None else None,
        minutes_sooner=sooner,
        baseline_breach_hours=a.breach_ticks * TICK_MINUTES / 60,
        triagex_breach_hours=b.breach_ticks * TICK_MINUTES / 60,
        headline=headline,
    )


def _impact(run: Run, curves: Curves) -> Impact:
    c = run.comparison
    last = curves.points[-1]
    has_effect = any(r.confirmed_staff > 0 for r in run.staffing_requests.values()) or bool(
        run.level_loading
    )
    primary = _hero(
        "ambulance_handoff_hours",
        "Ambulance hours lost to handoff delays",
        "ambulance hours",
        _delta(last.baseline_ambulance_hours_cumulative, last.triagex_ambulance_hours_cumulative),
        ("fewer ambulance hours lost", "more ambulance hours lost"),
        decimals=2,  # always a multiple of 0.25; avoids half-even rounding (9.25 -> "9.2")
        intervention_scheduled=has_effect,
    )
    secondary = [
        _hero("mean_wait_minutes", "Average patient wait", "min",
              c.window_deltas["mean_wait_minutes"],
              ("min shorter average wait", "min longer average wait"),
              intervention_scheduled=has_effect),
        _hero("diverted_ambulances", "Ambulances diverted", "ambulances",
              c.window_deltas["diverted_ambulances"],
              ("fewer ambulances diverted", "more ambulances diverted"), decimals=0,
              intervention_scheduled=has_effect),
    ]  # fmt: skip
    return Impact(
        state="intervention_scheduled" if has_effect else "awaiting_intervention",
        primary=primary,
        secondary=secondary,
        recovery=_recovery(run),
        window=curves.window,
    )


def _transitions(run: Run) -> list[StatusTransition]:
    out = []
    for track_name, track in (("baseline", run.comparison.baseline),
                              ("triagex", run.comparison.triagex)):  # fmt: skip
        for hid in track.hospital_ids:
            series = track.hospital_series(hid)
            for prev, cur in zip(series, series[1:], strict=False):
                if prev.status is not cur.status:
                    out.append(
                        StatusTransition(
                            tick=cur.tick,
                            clock=run.scenario.clock_label(cur.tick),
                            track=track_name,
                            hospital_id=hid,
                            from_status=prev.status.value,
                            to_status=cur.status.value,
                        )  # fmt: skip
                    )
    return sorted(out, key=lambda x: (x.tick, x.track, x.hospital_id))


def _flows(run: Run) -> list[Flow]:
    groups: dict[tuple, int] = defaultdict(int)
    travel: dict[tuple, int] = {}
    for track_name, track in (("baseline", run.comparison.baseline),
                              ("triagex", run.comparison.triagex)):  # fmt: skip
        for r in track.patients:
            if r.reroute_reason is None or not (0 <= r.original_arrival_tick < track.horizon_ticks):
                continue
            kind = "diversion" if r.reroute_reason is RerouteReason.DIVERSION else "level_loading"
            key = (r.original_arrival_tick, track_name, r.intended_hospital_id,
                   r.final_hospital_id, kind)  # fmt: skip
            groups[key] += 1
            travel[key] = r.extra_travel_ticks
    return [
        Flow(
            tick=k[0],
            track=k[1],
            from_hospital_id=k[2],
            to_hospital_id=k[3],
            kind=k[4],
            count=n,
            travel_ticks=travel[k],
        )  # fmt: skip
        for k, n in sorted(groups.items())
    ]


def _proposal_title(run: Run, kind: str, hospital_id: str, staff: int | None) -> str:
    name = run.network.get(hospital_id).short_name
    if kind == "staffing":
        return f"Call in {staff} staff at {name}"
    return f"Level-load ambulances away from {name}"


def _cards(run: Run, only_window: bool) -> list[ProposalCard]:
    recommended = run.recommended_proposal()
    start, end = run.scenario.demo_window
    seen: dict[str, ProposalCard] = {}
    for p in [*run.comparison.triagex.proposals, *run.baseline.proposals]:
        if p.id in seen:
            continue
        approved = p.id in run.approved_proposals
        is_recommended = recommended is not None and p.id == recommended.id
        if only_window and not (approved or is_recommended) and not (start <= p.tick < end):
            continue
        if p.id not in {q.id for q in run.comparison.triagex.proposals} and not approved:
            continue  # no longer raised in the operator's (TriageX) future
        seen[p.id] = ProposalCard(
            id=p.id, kind=p.kind, hospital_id=p.hospital_id, tick=p.tick,
            clock=run.scenario.clock_label(p.tick),
            title=_proposal_title(run, p.kind, p.hospital_id, p.recommended_staff),
            rationale=p.rationale, forecast_saturation_pct=p.forecast_saturation_pct,
            current_saturation_pct=p.current_saturation_pct,
            recommended_staff=p.recommended_staff,
            status="approved" if approved else "available",
            recommended=recommended is not None and p.id == recommended.id,
        )  # fmt: skip
    return sorted(seen.values(), key=lambda x: (x.tick, x.hospital_id, x.kind))


def _requests(service: DemoService, run: Run) -> list[StaffingRequestView]:
    out = []
    for r in run.staffing_requests.values():
        effect = next(
            (e for e in run.comparison.intervention_effects if e.intervention_id == r.request_id),
            None,
        )
        iv = r.intervention()
        out.append(
            StaffingRequestView(
                request_id=r.request_id,
                proposal_id=r.proposal_id,
                hospital_id=r.hospital_id,
                requested_staff=r.requested_staff,
                confirmed_staff=r.confirmed_staff,
                usable_staff=effect.usable_staff if effect and effect.usable_staff else 0,
                status=service.request_status(r),
                proposal_tick=r.proposal_tick,
                proposal_clock=run.scenario.clock_label(r.proposal_tick),
                approved_tick=r.approved_tick,
                approved_clock=run.scenario.clock_label(r.approved_tick),
                effective_tick=iv.effective_tick,
                effective_clock=run.scenario.clock_label(iv.effective_tick),
                end_tick=iv.end_tick,
                approved_at=r.approved_at,
                expires_at=r.expires_at,
                confirmations=[
                    Confirmation(
                        staff_id=c.staff_id,
                        display_name=c.display_name,
                        source=c.source,
                        received_at=c.received_at,
                    )
                    for c in r.confirmations
                ],
            )  # fmt: skip
        )
    return out


def _feed(run: Run, divergence: int | None) -> list[FeedEvent]:
    sc, c, net = run.scenario, run.comparison, run.network
    label = sc.clock_label
    events: list[tuple[int, int, FeedEvent]] = []

    def add(rank: int, **kw) -> None:
        tick = kw["tick"]
        kw.setdefault("clock", label(tick))
        kw.setdefault("id", f"{kw['kind']}:{tick}:{kw.get('hospital_id') or ''}:{len(events)}")
        events.append((tick, rank, FeedEvent(**kw)))

    add(
        0,
        tick=sc.surge_onset_tick,
        title="Surge Begins",
        detail=sc.name,
        kind="surge_onset",
        track="both",
        origin="simulation",
        severity="warning",
    )
    for p in c.triagex.proposals:
        if p.kind != "staffing":
            continue
        name = net.get(p.hospital_id).short_name
        add(1, tick=p.tick, title="Surge Predicted",
            detail=f"{name}: 2-hour forecast {p.forecast_saturation_pct:.0f}% of treatment spaces",
            kind="surge_predicted", track="triagex", origin="simulation",
            severity="critical" if p.forecast_saturation_pct >= 100 else "warning",
            hospital_id=p.hospital_id)  # fmt: skip
    recommended = run.recommended_proposal()
    if recommended is not None:
        add(1, tick=recommended.tick, title="Outreach Proposed",
            detail=_proposal_title(run, recommended.kind, recommended.hospital_id,
                                   recommended.recommended_staff),
            kind="outreach_proposed", track="triagex", origin="simulation", severity="warning",
            hospital_id=recommended.hospital_id)  # fmt: skip
    for a in run.actions:
        name = net.get(a.hospital_id).short_name
        if a.kind == "outreach_approved":
            title = "Outreach Approved"
            detail = (
                f"Staff outreach approved for {name}; confirmed staff on the floor by "
                f"{label(a.tick + STAFF_LEAD_TICKS)}"
            )
        else:
            title, detail = "Level-Loading Approved", f"Redirect low-acuity ambulances from {name}"
        add(
            2,
            tick=a.tick,
            title=title,
            detail=detail,
            kind=a.kind,
            track="triagex",
            origin="operator",
            severity="info",
            hospital_id=a.hospital_id,
            wall_time=a.wall_time,
        )
    for r in run.staffing_requests.values():
        name = net.get(r.hospital_id).short_name
        for i, conf in enumerate(r.confirmations, start=1):
            add(
                3,
                tick=r.approved_tick,
                title="Staff Confirmed",
                detail=(
                    f"{conf.display_name} confirmed for {name} ({i}/{r.requested_staff})"
                    + (" via simulated reply" if conf.source != "webhook" else "")
                ),
                kind="staff_confirmed",
                track="triagex",
                origin="webhook" if conf.source == "webhook" else "simulated_reply",
                severity="success",
                hospital_id=r.hospital_id,
                wall_time=conf.received_at,
            )
            add(3, tick=r.approved_tick, title="Capacity Recalculated",
                detail=f"With TriageX future recalculated in {conf.recompute_ms:.0f} ms",
                kind="recalculated", track="triagex", origin="simulation", severity="info",
                hospital_id=r.hospital_id, wall_time=conf.received_at)  # fmt: skip
        if r.confirmed_staff:
            iv = r.intervention()
            add(3, tick=r.approved_tick, title="Staff En Route",
                detail=(f"{r.confirmed_staff} confirmed staff heading to {name}; "
                        f"shift starts {label(iv.effective_tick)}"),
                kind="staff_en_route", track="triagex", origin="simulation", severity="info",
                hospital_id=r.hospital_id)  # fmt: skip
    # Diversion events identical in both futures are shown once, as "both".
    diversion_keys = {
        name: {(e.tick, e.kind, e.hospital_id) for e in track.events
               if e.kind in (EventKind.DIVERSION_START, EventKind.DIVERSION_END)}
        for name, track in (("baseline", c.baseline), ("triagex", c.triagex))
    }  # fmt: skip
    shared = diversion_keys["baseline"] & diversion_keys["triagex"]
    for track_name, track in (("baseline", c.baseline), ("triagex", c.triagex)):
        side = "Without TriageX" if track_name == "baseline" else "With TriageX"
        for e in track.events:
            name = net.get(e.hospital_id).short_name
            if e.kind in (EventKind.DIVERSION_START, EventKind.DIVERSION_END):
                key = (e.tick, e.kind, e.hospital_id)
                if key in shared and track_name == "triagex":
                    continue
                both = key in shared
                prefix = "" if both else f"{side}: "
                starting = e.kind is EventKind.DIVERSION_START
                add(5, tick=e.tick,
                    title="Diversion Started" if starting else "Diversion Ended",
                    detail=f"{prefix}{name} {'diverts ambulances' if starting else 'reopens'}",
                    kind="diversion_start" if starting else "diversion_end",
                    track="both" if both else track_name, origin="simulation",
                    severity="critical" if starting else "info",
                    hospital_id=e.hospital_id)  # fmt: skip
            elif track_name == "triagex" and e.kind is EventKind.STAFF_SHIFT_ACTIVE:
                add(6, tick=e.tick, title="Shift Active",
                    detail=f"{e.detail['added_staff']} confirmed staff on the floor at {name}",
                    kind="shift_active", track="triagex", origin="simulation",
                    severity="success", hospital_id=e.hospital_id)  # fmt: skip
            elif track_name == "triagex" and e.kind is EventKind.STAFF_SHIFT_ENDED:
                add(6, tick=e.tick, title="Shift Ended", detail=f"Surge shift at {name} ends",
                    kind="shift_end", track="triagex", origin="simulation", severity="info",
                    hospital_id=e.hospital_id)  # fmt: skip
            elif track_name == "triagex" and e.kind is EventKind.LEVEL_LOADING_ACTIVE:
                add(6, tick=e.tick, title="Level-Loading Active",
                    detail=f"Low-acuity ambulances redirected from {name}",
                    kind="level_loading_active", track="triagex", origin="simulation",
                    severity="success", hospital_id=e.hospital_id)  # fmt: skip
    if divergence is not None:
        add(7, tick=divergence, title="Futures Diverge",
            detail="The two simulated futures separate from here", kind="divergence",
            track="both", origin="simulation", severity="success")  # fmt: skip
    rec = _recovery(run)
    for track_name, tick in (("baseline", rec.baseline_recovered_tick),
                             ("triagex", rec.triagex_recovered_tick)):  # fmt: skip
        if tick is not None:
            side = "Without TriageX" if track_name == "baseline" else "With TriageX"
            name = net.get(rec.hospital_id).short_name
            add(8, tick=tick, title="Back to Normal", detail=f"{side}: {name} below 85%",
                kind="recovered", track=track_name, origin="simulation", severity="success",
                hospital_id=rec.hospital_id)  # fmt: skip
    events.sort(key=lambda x: (x[0], x[1]))  # stable: keeps confirm/recalculate pairs together
    start, end = sc.demo_window
    for _, _, e in events:
        e.in_demo_window = start <= e.tick < end
    return [e for _, _, e in events]


def _bands(run: Run) -> list[Band]:
    start, end = run.scenario.demo_window
    bands = [Band(kind="demo_window", start_tick=start, end_tick=end, label="6-hour demo window")]
    for r in run.staffing_requests.values():
        iv = r.intervention()
        bands.append(Band(kind="staff_lead_time", start_tick=iv.approved_tick,
                          end_tick=iv.effective_tick, label="Staff en route (45 min)",
                          hospital_id=r.hospital_id))  # fmt: skip
        if r.confirmed_staff:
            bands.append(Band(kind="shift_active", start_tick=iv.effective_tick,
                              end_tick=iv.end_tick, label="Surge shift active",
                              hospital_id=r.hospital_id))  # fmt: skip
    for iv in run.level_loading.values():
        bands.append(Band(kind="level_loading_active", start_tick=iv.effective_tick,
                          end_tick=iv.end_tick, label="Level-loading active",
                          hospital_id=iv.hospital_id))  # fmt: skip
    return bands


MARKER_LABELS = {
    "surge_onset": "Surge Begins",
    "proposal": "Surge Predicted",
    "approval": "Outreach Approved",
    "shift_active": "Shift Active",
    "shift_end": "Shift Ended",
    "level_loading_active": "Level-Loading Active",
    "level_loading_end": "Level-Loading Ended",
    "divergence": "Futures Diverge",
    "baseline_diversion": "Diversion (without TriageX)",
}


def _markers(run: Run) -> list[Marker]:
    out = []
    for m in run.comparison.markers:
        if m.kind in ("demo_window_start", "demo_window_end"):
            continue
        if m.kind == "proposal" and "staffing" not in (m.intervention_id or ""):
            continue
        if m.kind == "approval" and m.intervention_id in run.level_loading:
            label = "Level-Loading Approved"
        else:
            label = MARKER_LABELS.get(m.kind, m.label)
        start, end = run.scenario.demo_window
        out.append(Marker(tick=m.tick, clock=m.clock, kind=m.kind, label=label,
                          hospital_id=m.hospital_id,
                          in_demo_window=start <= m.tick < end))  # fmt: skip
    return out


def build_dashboard(service: DemoService, run: Run) -> DashboardState:
    started = time.perf_counter()
    sc, c = run.scenario, run.comparison
    divergence = c.first_divergence_tick
    curves = _curves(run, divergence)
    state = DashboardState(
        run=RunInfo(
            run_id=run.run_id,
            version=run.version,
            seed=run.seed,
            created_at=run.created_at,
            notice=SYNTHETIC_DATA_NOTICE,
            scenario=ScenarioSummary(
                id=sc.id,
                name=sc.name,
                description=sc.description,
                focus_hospital_id=sc.focus_hospital_id,
                surge_onset_tick=sc.surge_onset_tick,
            ),
        ),  # fmt: skip
        timeline=Timeline(
            tick_minutes=TICK_MINUTES,
            horizon_ticks=sc.horizon_ticks,
            clocks=[sc.clock_label(t) for t in range(sc.horizon_ticks)],
            demo_window=_window(run),
            suggested_ms_per_tick=SUGGESTED_MS_PER_TICK,
        ),  # fmt: skip
        hospitals=[
            HospitalInfo(
                id=h.id,
                name=h.name,
                short_name=h.short_name,
                lat=h.lat,
                lon=h.lon,
                treatment_spaces=h.treatment_spaces,
                base_staff=h.base_staff,
            )
            for h in run.network.hospitals
        ],  # fmt: skip
        series=[
            HospitalSeries(
                hospital_id=hid,
                baseline=[_point(r) for r in c.baseline.hospital_series(hid)],
                triagex=[_point(r) for r in c.triagex.hospital_series(hid)],
            )
            for hid in c.baseline.hospital_ids
        ],
        curves=curves,
        bands=_bands(run),
        markers=_markers(run),
        transitions=_transitions(run),
        flows=_flows(run),
        feed=_feed(run, divergence),
        proposals=_cards(run, only_window=True),
        staffing_requests=_requests(service, run),
        impact=_impact(run, curves),
        divergence=Divergence(tick=divergence, clock=sc.clock_label(divergence))
        if divergence is not None
        else None,
        meta=Meta(generated_at=datetime.now(UTC), recompute_ms=run.last_recompute_ms, build_ms=0),
    )
    state.meta.build_ms = round((time.perf_counter() - started) * 1000, 2)
    return state


def _forecast_accuracy(track: TrackResult) -> ForecastAccuracy:
    errors = []
    for hid in track.hospital_ids:
        s = track.hospital_series(hid)
        for t in range(len(s) - FORECAST_HORIZON_TICKS):
            errors.append(
                s[t].forecast_saturation_pct - s[t + FORECAST_HORIZON_TICKS].saturation_pct
            )
    n = len(errors) or 1
    return ForecastAccuracy(
        horizon_minutes=FORECAST_HORIZON_TICKS * TICK_MINUTES,
        samples=len(errors),
        bias_pct_points=round(sum(errors) / n, 2),
        mean_absolute_error_pct_points=round(sum(abs(e) for e in errors) / n, 2),
    )


def build_diagnostics(run: Run) -> Diagnostics:
    c = run.comparison

    def jsonable(obj):
        return {k: (v.value if hasattr(v, "value") else v) for k, v in asdict(obj).items()}

    return Diagnostics(
        run_id=run.run_id,
        version=run.version,
        scenario_assumptions=list(run.scenario.assumptions),
        model_limitations=list(MODEL_LIMITATIONS),
        forecast_accuracy=_forecast_accuracy(run.baseline),
        all_proposals=_cards(run, only_window=False),
        intervention_effects=[asdict(e) for e in c.intervention_effects],
        full_horizon_metrics={"baseline": jsonable(c.baseline_metrics),
                              "triagex": jsonable(c.triagex_metrics)},
        demo_window_metrics={"baseline": jsonable(c.window_baseline_metrics),
                             "triagex": jsonable(c.window_triagex_metrics)},
        track_digests={"baseline": track_digest(c.baseline), "triagex": track_digest(c.triagex)},
        recompute_history_ms=list(run.recompute_history_ms),
    )  # fmt: skip
