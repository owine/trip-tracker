"""Application settings loaded from environment variables.

Two-class split:

- `WorkerSettings` — the subset of config the saq worker needs (DB, Redis,
  LLM, Meili, documents storage, logging). Worker boots with these alone.
- `Settings(WorkerSettings)` — full app config; adds single-owner auth/sessions
  /base URL/webhook/UI-bound fields. App boots with all of them.

The split prevents the "every container needs every secret" deploy footgun:
the worker no longer requires session/OIDC/webhook env vars to start, since
it never serves HTTP routes that consume them. `Settings` is a strict
superset of `WorkerSettings` (covariance via inheritance), so any function
typed `WorkerSettings` accepts a `Settings` instance — but not vice versa.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_RESERVED_HEADER_RE = re.compile(
    r"^(authorization|cookie|host|content-length|content-type|x-forwarded-.*)$",
    re.IGNORECASE,
)


class WorkerSettings(BaseSettings):
    """Worker-side configuration. Boots with only DB/Redis/LLM/Meili/docs/log
    env vars set. The full `Settings` class extends this with app-only fields."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Database
    database_url: str = Field(..., description="postgresql+asyncpg://...")

    # Logging (worker uses the same log config as app)
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "console"

    # Phase 3 — parser pipeline (worker runs the parsers)
    anthropic_api_key: SecretStr
    redis_url: str
    llm_daily_budget_cents: int = 100  # $1.00 USD/day soft cap
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_confidence_floor: float = 0.7

    # Phase 4 — search (worker syncs to Meili after parse)
    meili_url: str
    meili_master_key: SecretStr

    # Phase 5 — documents storage path (worker writes email attachments here)
    documents_dir: Path = Path("/data/documents")

    @field_validator("database_url")
    @classmethod
    def _validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return v

    @field_validator("llm_confidence_floor")
    @classmethod
    def _floor_in_unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("llm_confidence_floor must be in [0, 1]")
        return v


class Settings(WorkerSettings):
    """Full application settings. Extends `WorkerSettings` with app-only fields
    (sessions, single-owner auth, base URL, webhook secrets, UI bounds). Required
    values raise on startup if missing."""

    # Sessions (app-only — worker doesn't issue cookies)
    session_secret: SecretStr = Field(..., min_length=32)
    session_cookie_name: str = "tt_session"
    session_max_age_seconds: int = 7 * 24 * 60 * 60  # 7 days

    # Single-owner auth (replaces OIDC)
    owner_email: str = Field(
        ...,
        description="Email address of the single owner; seeded into users table on first boot.",
    )
    owner_session_token: str = Field(
        ...,
        min_length=32,
        description="Shared secret presented at /auth/bootstrap?token=<>. >=32 chars.",
    )

    # App URL
    base_url: str

    # Webhook (forwardemail.net) — app-only; only ingest routes consume these
    webhook_secret: SecretStr = Field(...)
    forwardemail_relay_token: SecretStr = Field(
        ...,
        description="Shared secret for the ForwardEmail webhook adapter. "
        "Compared with the ?token= query param via hmac.compare_digest.",
    )
    webhook_signature_header: str = Field(default="X-Webhook-Signature")
    webhook_timestamp_tolerance_seconds: int = Field(default=300)
    webhook_max_body_bytes: int = Field(default=26_214_400)  # 25 MiB

    # Documents — UI upload bound + X-Accel-Redirect header (app-only)
    max_upload_bytes: int = 26_214_400  # 25 MiB
    documents_x_accel_prefix: str | None = None

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
