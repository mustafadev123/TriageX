"""Environment-based configuration. All secrets stay server-side."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

APP_NAME = "TriageX"
TAGLINE = "Every ER capacity tool shows you the problem. TriageX calls the people who can fix it."
SYNTHETIC_DATA_NOTICE = "Synthetic hospital operations simulation — not real-time hospital data."
DEMO_MESSAGE_PREFIX = "TriageX DEMO"
ENGINE_VERSION = "0.4.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"

    # Tiger Data (Phase 4). Empty or unset = run without a database.
    database_url: str | None = None
    database_schema: str | None = None  # defaults to the server's search_path (public)
    database_pool_size: int = 3
    database_timeout_seconds: float = 10.0

    # AI providers (Phase 5)
    openai_api_key: str | None = None
    openai_model: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    ai_primary_provider: str = "openai"
    ai_fallback_provider: str = "gemini"

    # Chert (Phase 6)
    chert_signing_secret: str | None = None
    chert_tenant: str | None = None
    chert_webhook_secret: str | None = None
    demo_allowed_phones: str = ""
    public_base_url: str | None = None
    chert_api_base: str | None = None  # defaults to https://console.trychert.com/api/v1

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
