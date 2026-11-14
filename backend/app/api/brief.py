"""AI operational brief: POST /api/runs/{run_id}/brief {at_tick}, GET the latest one."""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.chert import _submit
from app.repositories.brief import BriefRow
from app.services.brief import Brief, BriefService
from app.services.dashboard import build_dashboard
from app.services.demo_service import NotFoundError

router = APIRouter(tags=["brief"])
log = logging.getLogger("triagex.brief.api")


class BriefRequest(BaseModel):
    at_tick: int = Field(ge=0, description="The playhead: nothing after it is used (no spoilers)")


def _briefs(request: Request) -> BriefService:
    return request.app.state.briefs


def _out(b: Brief) -> dict:
    d = asdict(b)
    d["generated_at"] = b.generated_at.isoformat()
    return d


@router.post("/runs/{run_id}/brief")
async def create_brief(request: Request, run_id: str, body: BriefRequest) -> dict:
    service = request.app.state.demo_service
    try:
        run = service.get_run(run_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    state = build_dashboard(service, run)
    brief = await run_in_threadpool(_briefs(request).generate, state, body.at_tick)
    _submit(request.app, BriefRow.of(brief))
    return _out(brief)


@router.get("/runs/{run_id}/brief")
async def latest_brief(request: Request, run_id: str) -> dict:
    brief = _briefs(request).latest(run_id)
    if brief is None:
        raise HTTPException(404, "No brief generated for this run yet")
    return _out(brief)
