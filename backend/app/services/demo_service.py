"""In-memory demo runs: scenario state, operator approvals, and staff confirmations.

The baseline track and the patient stream are computed once per run and never change.
Each staff confirmation re-simulates Track B only, with the same patients, then the
comparison is rebuilt. History is never rewritten: a confirmation's simulated effect
starts at the approved request time plus the staffing lead time, regardless of when the
reply arrives in real time.

Phase 4 moves persistence to Tiger Data; this store is process-local.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.simulation.arrivals import generate_patients
from app.simulation.comparison import Comparison, build_comparison
from app.simulation.engine import simulate
from app.simulation.hospitals import DEFAULT_NETWORK, Network
from app.simulation.interventions import (
    Intervention,
    LevelLoadingIntervention,
    StaffingIntervention,
)
from app.simulation.metrics import Metrics, summarize
from app.simulation.models import Proposal, Track, TrackResult
from app.simulation.scenarios import Scenario, get_scenario

log = logging.getLogger("triagex.demo")

# Sources whose confirmations come from a real person through a live channel. They carry the
# recipient's identity, so the same person can never confirm one request twice.
LIVE_SOURCES = frozenset({"webhook", "imessage", "facetime"})

DEFAULT_SCENARIO_ID = "i85_pileup"
DEFAULT_SEED = 42
DEFAULT_REQUEST_TTL = timedelta(minutes=10)

# Fictional demo staff. Phase 6 maps opted-in teammates' phones onto these identities.
DEMO_STAFF: tuple[tuple[str, str], ...] = (
    ("rn-avery", "Avery (fictional RN)"),
    ("rn-jordan", "Jordan (fictional RN)"),
    ("rn-morgan", "Morgan (fictional RN)"),
    ("rn-riley", "Riley (fictional RN)"),
    ("rn-casey", "Casey (fictional RN)"),
    ("rn-quinn", "Quinn (fictional RN)"),
)
STAFF_NAMES = dict(DEMO_STAFF)


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class ConfirmationRecord:
    staff_id: str
    display_name: str
    source: str  # "simulated" (dashboard fallback), "webhook" / "imessage" (verified Chert
    # reply) or "facetime" (live FaceTime card check plus verbal confirmation)
    received_at: datetime
    recompute_ms: float


@dataclass(slots=True)
class StaffingRequest:
    request_id: str
    proposal_id: str
    hospital_id: str
    requested_staff: int
    proposal_tick: int  # simulated time the forecast raised the proposal
    approved_tick: int  # simulated time the operator approved; the lead time starts here
    approved_at: datetime  # wall-clock time of the approval
    expires_at: datetime
    operator: str
    confirmations: list[ConfirmationRecord] = field(default_factory=list)

    @property
    def confirmed_staff(self) -> int:
        return len(self.confirmations)

    def intervention(self) -> StaffingIntervention:
        return StaffingIntervention(
            id=self.request_id,
            hospital_id=self.hospital_id,
            approved_tick=self.approved_tick,
            confirmed_staff=self.confirmed_staff,
        )


@dataclass(slots=True)
class OperatorAction:
    kind: str  # "outreach_approved" | "level_loading_approved"
    tick: int
    wall_time: datetime
    hospital_id: str
    proposal_id: str
    operator: str


@dataclass(slots=True)
class Run:
    run_id: str
    scenario: Scenario
    seed: int
    created_at: datetime
    network: Network
    patients: tuple
    baseline: TrackResult
    baseline_metrics: tuple[Metrics, Metrics]
    comparison: Comparison
    version: int = 1
    staffing_requests: dict[str, StaffingRequest] = field(default_factory=dict)
    level_loading: dict[str, LevelLoadingIntervention] = field(default_factory=dict)
    approved_proposals: set[str] = field(default_factory=set)
    actions: list[OperatorAction] = field(default_factory=list)
    recompute_history_ms: list[float] = field(default_factory=list)
    last_recompute_ms: float | None = None

    def interventions(self) -> tuple[Intervention, ...]:
        staffing = [r.intervention() for r in self.staffing_requests.values()]
        return (*staffing, *self.level_loading.values())

    def find_proposal(self, proposal_id: str) -> Proposal:
        for source in (self.comparison.triagex.proposals, self.baseline.proposals):
            for p in source:
                if p.id == proposal_id:
                    return p
        raise NotFoundError(f"Proposal '{proposal_id}' not found")

    def recommended_proposal(self) -> Proposal | None:
        """The demo's headline proposal: first surge-time staffing proposal at the focus
        hospital (falling back to the first surge-time staffing proposal anywhere)."""
        onset = self.scenario.surge_onset_tick
        start, end = self.scenario.demo_window
        staffing = [p for p in self.baseline.proposals if p.kind == "staffing" and p.tick >= onset]
        focus = [p for p in staffing if p.hospital_id == self.scenario.focus_hospital_id]
        in_window = [p for p in staffing if start <= p.tick < end]
        focus_in_window = [p for p in focus if start <= p.tick < end]
        for candidates in (focus_in_window, in_window, focus, staffing):
            if candidates:
                return candidates[0]
        return None

    def recompute(self) -> float:
        started = time.perf_counter()
        triagex = simulate(
            self.network, self.scenario, self.patients, self.interventions(), Track.TRIAGEX
        )
        self.comparison = build_comparison(
            self.scenario,
            self.seed,
            self.patients,
            self.baseline,
            triagex,
            self.network,
            baseline_metrics=self.baseline_metrics,
        )
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        self.last_recompute_ms = elapsed
        self.recompute_history_ms.append(elapsed)
        return elapsed


class DemoService:
    def __init__(
        self,
        network: Network = DEFAULT_NETWORK,
        request_ttl: timedelta = DEFAULT_REQUEST_TTL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.network = network
        self.request_ttl = request_ttl
        self.clock = clock
        self._lock = threading.RLock()
        self._runs: dict[str, Run] = {}
        self._active_run_id: str | None = None
        # Set by OutreachService; lets the dashboard show live-messaging state per request.
        self.messaging = None
        # Set by FaceTimeService; lets the dashboard show live FaceTime calls and card checks.
        self.facetime = None

    # --- runs -----------------------------------------------------------------------
    def create_run(self, scenario_id: str = DEFAULT_SCENARIO_ID, seed: int = DEFAULT_SEED) -> Run:
        scenario = get_scenario(scenario_id)
        started = time.perf_counter()
        patients = generate_patients(self.network, scenario, seed)
        baseline = simulate(self.network, scenario, patients, (), Track.BASELINE)
        baseline_metrics = (
            summarize(baseline),
            summarize(baseline, tick_range=scenario.demo_window),
        )
        triagex = simulate(self.network, scenario, patients, (), Track.TRIAGEX)
        comparison = build_comparison(
            scenario, seed, patients, baseline, triagex, self.network, baseline_metrics
        )
        run = Run(
            run_id=uuid.uuid4().hex[:12],
            scenario=scenario,
            seed=seed,
            created_at=self.clock(),
            network=self.network,
            patients=patients,
            baseline=baseline,
            baseline_metrics=baseline_metrics,
            comparison=comparison,
        )
        run.last_recompute_ms = round((time.perf_counter() - started) * 1000, 2)
        with self._lock:
            self._runs[run.run_id] = run
            self._active_run_id = run.run_id
        log.info("run created", extra={"run_id": run.run_id, "scenario": scenario_id, "seed": seed})
        return run

    def reset(self, scenario_id: str = DEFAULT_SCENARIO_ID, seed: int = DEFAULT_SEED) -> Run:
        """Discard every run and start the demo again from the same initial state."""
        with self._lock:
            self._runs.clear()
            self._active_run_id = None
            return self.create_run(scenario_id, seed)

    def get_run(self, run_id: str) -> Run:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise NotFoundError(f"Run '{run_id}' not found") from None

    def active_run(self) -> Run:
        with self._lock:
            if self._active_run_id is None:
                return self.create_run()
            return self._runs[self._active_run_id]

    # --- operator actions -----------------------------------------------------------
    def approve_proposal(
        self, run_id: str, proposal_id: str, at_tick: int, operator: str = "operator"
    ) -> tuple[Run, str]:
        """Approve a proposal at simulated tick `at_tick` (the playhead when the operator
        clicked). The 45-minute staffing lead time starts at `at_tick`, never at the
        proposal tick. Returns the run and the created request/intervention id."""
        with self._lock:
            run = self.get_run(run_id)
            proposal = run.find_proposal(proposal_id)
            if proposal_id in run.approved_proposals:
                raise ConflictError(f"Proposal '{proposal_id}' is already approved")
            tick = at_tick
            if not (proposal.tick <= tick < run.scenario.horizon_ticks):
                raise ConflictError(
                    f"Approval tick {tick} must be between the proposal tick {proposal.tick} "
                    f"and the end of the horizon"
                )
            now = self.clock()
            if proposal.kind == "staffing":
                request_id = f"req-{uuid.uuid4().hex[:8]}"
                run.staffing_requests[request_id] = StaffingRequest(
                    request_id=request_id,
                    proposal_id=proposal.id,
                    hospital_id=proposal.hospital_id,
                    requested_staff=proposal.recommended_staff or 1,
                    proposal_tick=proposal.tick,
                    approved_tick=tick,
                    approved_at=now,
                    expires_at=now + self.request_ttl,
                    operator=operator,
                )
                created = request_id
                kind = "outreach_approved"
            else:
                created = f"ll-{uuid.uuid4().hex[:8]}"
                run.level_loading[created] = LevelLoadingIntervention(
                    id=created, source_hospital_id=proposal.hospital_id, approved_tick=tick
                )
                kind = "level_loading_approved"
                run.recompute()
            run.approved_proposals.add(proposal.id)
            run.actions.append(
                OperatorAction(kind, tick, now, proposal.hospital_id, proposal.id, operator)
            )
            run.version += 1
            log.info(
                "proposal approved",
                extra={"run_id": run.run_id, "proposal_id": proposal.id, "tick": tick},
            )
            return run, created

    def simulate_reply(
        self, run_id: str, request_id: str, staff_id: str | None = None, reply: str = "IN"
    ) -> tuple[Run, ConfirmationRecord]:
        """Dashboard fallback for a staff reply. Always labeled as simulated."""
        return self._record_reply(run_id, request_id, staff_id, reply, source="simulated")

    def confirm_live(
        self,
        run_id: str,
        request_id: str,
        staff_id: str,
        display_name: str,
        reply: str,
        source: str = "webhook",
    ) -> tuple[Run, ConfirmationRecord]:
        """A verified live confirmation (Chert iMessage reply or FaceTime card check plus
        verbal confirmation). Same validation and recompute as the fallback."""
        if source not in LIVE_SOURCES:
            raise ValueError(f"'{source}' is not a live confirmation source")
        return self._record_reply(
            run_id, request_id, staff_id, reply, source=source, display_name=display_name
        )

    def touch(self, run_id: str) -> Run:
        """Bump the version after a change outside the simulation (e.g. outreach sent), so
        dashboards refetch and the change is persisted as its own version."""
        with self._lock:
            run = self.get_run(run_id)
            run.version += 1
            return run

    def _record_reply(
        self,
        run_id: str,
        request_id: str,
        staff_id: str | None,
        reply: str,
        source: str,
        display_name: str | None = None,
    ) -> tuple[Run, ConfirmationRecord]:
        with self._lock:
            run = self.get_run(run_id)
            request = run.staffing_requests.get(request_id)
            if request is None:
                raise NotFoundError(f"Staffing request '{request_id}' not found")
            if reply.strip().upper() != "IN":
                raise ConflictError("Only the reply 'IN' confirms a demo shift")
            now = self.clock()
            if now >= request.expires_at:
                raise ConflictError(f"Staffing request '{request_id}' has expired")
            if request.confirmed_staff >= request.requested_staff:
                raise ConflictError(f"Staffing request '{request_id}' is already fully confirmed")
            confirmed_ids = {c.staff_id for c in request.confirmations}
            if source in LIVE_SOURCES:
                if not staff_id or not display_name:
                    raise ConflictError("A live confirmation needs a recipient identity")
                if staff_id in confirmed_ids:
                    raise ConflictError("This recipient has already confirmed this request")
            elif staff_id is None:
                staff_id = next((sid for sid, _ in DEMO_STAFF if sid not in confirmed_ids), None)
                if staff_id is None:
                    raise ConflictError("Every fictional demo staff member has already replied")
            elif staff_id not in STAFF_NAMES:
                raise NotFoundError(f"Unknown demo staff member '{staff_id}'")
            elif staff_id in confirmed_ids:
                raise ConflictError(f"'{staff_id}' has already confirmed this request")

            name = display_name if source in LIVE_SOURCES else STAFF_NAMES[staff_id]
            record = ConfirmationRecord(staff_id, name, source, now, 0.0)
            request.confirmations.append(record)
            record.recompute_ms = run.recompute()
            run.version += 1
            log.info(
                "staff confirmed",
                extra={
                    "run_id": run.run_id,
                    "request_id": request_id,
                    "source": source,
                    "confirmed": request.confirmed_staff,
                    "recompute_ms": record.recompute_ms,
                },
            )
            return run, record

    def request_status(self, request: StaffingRequest) -> str:
        if request.confirmed_staff >= request.requested_staff:
            return "fully_confirmed"
        if self.clock() >= request.expires_at:
            return "expired"
        return "partially_confirmed" if request.confirmed_staff else "awaiting_replies"
