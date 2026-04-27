"""Application settings loaded from environment variables."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Required values raise on startup if missing."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = Field(..., description="postgresql+asyncpg://...")

    # Sessions
    session_secret: SecretStr = Field(..., min_length=32)
    session_cookie_name: str = "tt_session"
    session_max_age_seconds: int = 7 * 24 * 60 * 60  # 7 days

    # OIDC
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: SecretStr
    oidc_redirect_uri: str
    admin_group: str = "trip-tracker:admin"

    # App
    base_url: str
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "console"

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return v
