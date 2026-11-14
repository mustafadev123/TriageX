from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.db import database_status
from app.config import APP_NAME, SYNTHETIC_DATA_NOTICE, TAGLINE

router = APIRouter(tags=["health"])

IntegrationStatus = Literal["ok", "starting", "not_configured", "not_implemented", "error"]


class HealthResponse(BaseModel):
    status: str
    app: str
    tagline: str
    notice: str


class IntegrationHealth(BaseModel):
    name: str
    status: IntegrationStatus
    detail: str


class IntegrationsResponse(BaseModel):
    simulation: IntegrationHealth
    database: IntegrationHealth
    ai_primary: IntegrationHealth
    ai_fallback: IntegrationHealth
    messaging: IntegrationHealth
    facetime: IntegrationHealth
    websocket_clients: int


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", app=APP_NAME, tagline=TAGLINE, notice=SYNTHETIC_DATA_NOTICE)


def _pending(name: str, configured: bool, phase: int) -> IntegrationHealth:
    # Never report an integration as healthy before it is implemented and verified.
    return IntegrationHealth(
        name=name,
        status="not_implemented",
        detail=f"Arrives in Phase {phase}"
        + (" (credentials present)" if configured else " (no credentials configured)"),
    )


def _database(request: Request) -> IntegrationHealth:
    status = database_status(request.app.state.persistence)
    if not status["configured"]:
        return IntegrationHealth(
            name="Tiger Data", status="not_configured", detail="DATABASE_URL is not set"
        )
    if not status["connected"]:
        return IntegrationHealth(name="Tiger Data", status="error", detail=status["error"] or "")
    writer = status["writer"]
    storage = "hypertable" if status["hypertable"] else "plain table (no TimescaleDB)"
    detail = (
        f"PostgreSQL {status['server_version']}, "
        f"TimescaleDB {status['timescaledb_version'] or 'not installed'}, metrics {storage}; "
        f"{writer['snapshots_written']} snapshots written, {writer['pending']} pending"
    )
    if writer["dead_letters"]:
        return IntegrationHealth(name="Tiger Data", status="error", detail=detail)
    if not writer["migrated"]:  # connected; background migration still running
        return IntegrationHealth(name="Tiger Data", status="starting", detail=detail)
    return IntegrationHealth(name="Tiger Data", status="ok", detail=detail)


def _messaging(request: Request) -> IntegrationHealth:
    # "ok" only after Chert accepted the credentials (GET /whoami) and the webhook secret,
    # allowlist, public URL, and a matching subscription are all in place.
    status, detail = request.app.state.outreach.health()
    return IntegrationHealth(name="Chert iMessage", status=status, detail=detail)


def _ai(request: Request, role: str) -> IntegrationHealth:
    # "ok" only after that provider actually produced a valid, grounded brief.
    status, detail = request.app.state.briefs.health(role)
    name = "AI brief (primary)" if role == "primary" else "AI brief (fallback)"
    return IntegrationHealth(name=name, status=status, detail=detail)


def _facetime(request: Request) -> IntegrationHealth:
    # "ok" only after the agent has actually called the backend with the right token.
    ft = request.app.state.facetime
    if not ft.enabled:
        return IntegrationHealth(name="FaceTime agent", status="not_configured",
                                 detail="TRIAGEX_AGENT_TOKEN is not set")  # fmt: skip
    if ft.last_contact_at is None:
        return IntegrationHealth(name="FaceTime agent", status="starting",
                                 detail="Token set; the agent has not called the backend yet")
    return IntegrationHealth(name="FaceTime agent", status="ok",
                             detail=f"Last agent request {ft.last_contact_at:%H:%M:%S} UTC")


@router.get("/integrations/health")
def integrations_health(request: Request) -> IntegrationsResponse:
    return IntegrationsResponse(
        simulation=IntegrationHealth(
            name="Simulation engine", status="ok", detail="Deterministic, in-process"
        ),
        database=_database(request),
        ai_primary=_ai(request, "primary"),
        ai_fallback=_ai(request, "fallback"),
        messaging=_messaging(request),
        facetime=_facetime(request),
        websocket_clients=request.app.state.ws_manager.count,
    )
