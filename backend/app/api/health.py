from fastapi import APIRouter
from pydantic import BaseModel

from app.config import APP_NAME, SYNTHETIC_DATA_NOTICE, TAGLINE

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    tagline: str
    notice: str


@router.get("/health")
def health() -> HealthResponse:
    """Liveness only. Integration health (DB, AI, Chert) arrives in Phases 4-6."""
    return HealthResponse(status="ok", app=APP_NAME, tagline=TAGLINE, notice=SYNTHETIC_DATA_NOTICE)
