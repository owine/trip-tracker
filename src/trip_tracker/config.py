"""Application settings loaded from environment variables."""

from __future__ import annotations

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_RESERVED_HEADER_RE = re.compile(
    r"^(authorization|cookie|host|content-length|content-type|x-forwarded-.*)$",
    re.IGNORECASE,
)


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

    # Webhook (forwardemail.net)
    webhook_secret: SecretStr = Field(...)
    webhook_signature_header: str = Field(default="X-Webhook-Signature")
    webhook_timestamp_tolerance_seconds: int = Field(default=300)
    webhook_max_body_bytes: int = Field(default=26_214_400)  # 25 MiB

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return v

    @field_validator("webhook_signature_header")
    @classmethod
    def _validate_signature_header(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        if _RESERVED_HEADER_RE.match(v):
            raise ValueError(f"{v!r} collides with a reserved/proxy header")
        return v

    @field_validator("webhook_timestamp_tolerance_seconds")
    @classmethod
    def _validate_tolerance(cls, v: int) -> int:
        if not 0 < v <= 3600:
            raise ValueError("must be in (0, 3600]")
        return v

    @field_validator("webhook_max_body_bytes")
    @classmethod
    def _validate_max_body(cls, v: int) -> int:
        if not 0 < v <= 100 * 1024 * 1024:
            raise ValueError("must be in (0, 100 MiB]")
        return v
