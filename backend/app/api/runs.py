"""Scenario control, operator approvals, simulated staff replies, and demo reset."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.models.dashboard import DashboardState, Diagnostics
from app.services.dashboard import build_dashboard, build_diagnostics
from app.services.demo_service import (
    DEFAULT_SCENARIO_ID,
    DEFAULT_SEED,
    ConflictError,
    DemoService,
    NotFoundError,
)
from app.simulation.scenarios import SCENARIOS

router = APIRouter(tags=["runs"])


class RunRequest(BaseModel):
    scenario_id: str = Field(default=DEFAULT_SCENARIO_ID)
    seed: int = Field(default=DEFAULT_SEED, ge=0, le=2**31 - 1)


class ApproveRequest(BaseModel):
    at_tick: int = Field(
        ge=0,
        description=(
            "Simulated tick at which the operator approved (the playhead position). "
            "Required: the staffing lead time starts here, not at the proposal tick."
        ),
    )
    operator: str = Field(default="demo-operator", max_length=64)


class SimulatedReply(BaseModel):
    staff_id: str | None = Field(default=None, description="Defaults to the next demo staff")
    reply: str = Field(default="IN", max_length=32)


def _service(request: Request) -> DemoService:
    return request.app.state.demo_service


def _check_scenario(scenario_id: str) -> None:
    if scenario_id not in SCENARIOS:
        raise HTTPException(422, f"Unknown scenario '{scenario_id}'. Options: {list(SCENARIOS)}")


def _raise(exc: Exception) -> None:
    if isinstance(exc, NotFoundError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(409, str(exc)) from exc
    raise exc


async def _notify(request: Request, run, reason: str) -> None:
    await request.app.state.ws_manager.broadcast(
        {
            "type": "run.updated",
            "run_id": run.run_id,
            "version": run.version,
            "reason": reason,
            "recompute_ms": run.last_recompute_ms,
        }
    )


@router.post("/demo/reset")
async def reset_demo(request: Request, body: RunRequest | None = None) -> DashboardState:
    """Discard all runs and return the same initial scenario state every time."""
    body = body or RunRequest()
    _check_scenario(body.scenario_id)
    service = _service(request)
    run = service.reset(body.scenario_id, body.seed)
    await _notify(request, run, "reset")
    return build_dashboard(service, run)


@router.get("/demo/state")
def active_state(request: Request) -> DashboardState:
    service = _service(request)
    return build_dashboard(service, service.active_run())


@router.post("/runs")
async def create_run(request: Request, body: RunRequest) -> DashboardState:
    _check_scenario(body.scenario_id)
    service = _service(request)
    run = service.create_run(body.scenario_id, body.seed)
    await _notify(request, run, "run_created")
    return build_dashboard(service, run)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> DashboardState:
    service = _service(request)
    try:
        return build_dashboard(service, service.get_run(run_id))
    except NotFoundError as exc:
        _raise(exc)


@router.get("/runs/{run_id}/diagnostics")
def get_diagnostics(request: Request, run_id: str) -> Diagnostics:
    try:
        return build_diagnostics(_service(request).get_run(run_id))
    except NotFoundError as exc:
        _raise(exc)


@router.post("/runs/{run_id}/proposals/{proposal_id}/approve")
async def approve_proposal(
    request: Request, run_id: str, proposal_id: str, body: ApproveRequest
) -> DashboardState:
    service = _service(request)
    try:
        run, _ = service.approve_proposal(run_id, proposal_id, body.at_tick, body.operator)
    except (NotFoundError, ConflictError) as exc:
        _raise(exc)
    await _notify(request, run, "proposal_approved")
    return build_dashboard(service, run)


@router.post("/runs/{run_id}/staffing-requests/{request_id}/simulate-reply")
async def simulate_reply(
    request: Request, run_id: str, request_id: str, body: SimulatedReply | None = None
) -> DashboardState:
    """Dashboard fallback for a staff 'IN' reply. Recorded with source='simulated'."""
    body = body or SimulatedReply()
    service = _service(request)
    try:
        run, _ = service.simulate_reply(run_id, request_id, body.staff_id, body.reply)
    except (NotFoundError, ConflictError) as exc:
        _raise(exc)
    await _notify(request, run, "staff_confirmed")
    return build_dashboard(service, run)
