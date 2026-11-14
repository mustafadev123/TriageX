from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.config import APP_NAME, SYNTHETIC_DATA_NOTICE, TAGLINE, get_settings

router = APIRouter(tags=["health"])

IntegrationStatus = Literal["ok", "not_configured", "not_implemented", "error"]


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


@router.get("/integrations/health")
def integrations_health(request: Request) -> IntegrationsResponse:
    s = get_settings()
    return IntegrationsResponse(
        simulation=IntegrationHealth(
            name="Simulation engine", status="ok", detail="Deterministic, in-process"
        ),
        database=_pending("Tiger Data", bool(s.database_url), 4),
        ai_primary=_pending("OpenAI", bool(s.openai_api_key), 5),
        ai_fallback=_pending("Gemini", bool(s.gemini_api_key), 5),
        messaging=_pending("Chert", bool(s.chert_signing_secret), 6),
        websocket_clients=request.app.state.ws_manager.count,
    )
