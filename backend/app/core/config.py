"""Application settings, read from the environment.

One ``Settings`` instance is built at import and shared. Anything that varies
between a laptop, CI and the Hetzner box belongs here rather than inline, and
anything that would be *dangerous* to leave at its development default is
checked by :func:`validate_startup_config`, which the app calls on boot.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The development JWT secret. Named as a constant so the startup check can
# compare against it by identity of value rather than by a copy-pasted string.
DEV_JWT_SECRET = "dev-only-secret-change-me-in-any-real-deployment"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Application -----------------------------------------------------
    app_name: str = "CompliPilot"
    environment: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    log_level: str = "INFO"
    # "console" for a human at a terminal, "json" for the journal on the VPS.
    log_format: str = "console"

    # --- Database --------------------------------------------------------
    database_url: str = "postgresql+psycopg://complipilot:complipilot@localhost:5433/complipilot"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    # A query that runs longer than this is a bug, not a slow query. Set high
    # enough that a full-year calendar build for a CA firm with 100 clients
    # completes, low enough that a runaway one does not hold a connection open
    # until the pool is exhausted.
    db_statement_timeout_seconds: int = 30

    # --- Redis / queue ---------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    # Off in tests so a task call runs inline instead of hanging on a broker
    # that is not there.
    celery_enabled: bool = True

    # --- Auth ------------------------------------------------------------
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12
    refresh_token_expire_days: int = 30
    # Section 8.2 makes TOTP mandatory for Admin and Compliance Manager. The
    # flag exists so a development stack can be driven without an authenticator
    # app; the startup check refuses to let it stay false in production.
    require_totp_for_privileged_roles: bool = True

    # --- CORS ------------------------------------------------------------
    backend_cors_origins: str = "http://localhost:3000,http://localhost:3001"

    # --- Uploads ---------------------------------------------------------
    upload_dir: str = "data/documents"
    max_upload_mb: int = 25

    # --- OpenRouter ------------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Free-tier models, per the brief. The vision model is only reached for a
    # scanned notice with no extractable text layer.
    openrouter_model: str = "openai/gpt-oss-20b:free"
    openrouter_vision_model: str = "qwen/qwen2.5-vl-72b-instruct:free"
    openrouter_timeout_seconds: int = 120
    openrouter_max_retries: int = 2

    # --- Notifications ---------------------------------------------------
    # Days before a deadline at which a reminder goes out (section 4.1).
    reminder_offsets_days: str = "30,15,7,3,1"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "compliance@complipilot.in"
    smtp_use_tls: bool = True

    # WhatsApp Cloud API. Absent credentials degrade to a recorded-but-not-sent
    # notification rather than an exception, so a development stack still shows
    # the reminder pipeline working.
    whatsapp_api_url: str = "https://graph.facebook.com/v21.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""

    # --- Rate limiting ---------------------------------------------------
    rate_limit_enabled: bool = True
    # Per organization, per minute. Section 6.1 requires the limit be per-org
    # rather than per-IP: a CA firm behind one office NAT is one tenant.
    rate_limit_per_minute: int = 300

    @field_validator("environment")
    @classmethod
    def _normalise_environment(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "prod"}

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]

    @property
    def reminder_offsets(self) -> list[int]:
        """Reminder offsets, descending and de-duplicated.

        Descending order matters: the scheduler walks these from the furthest
        out to the nearest and stops at the first that has already fired, which
        only works if they are sorted.
        """
        seen: set[int] = set()
        for part in self.reminder_offsets_days.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value >= 0:
                seen.add(value)
        return sorted(seen, reverse=True)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def validate_startup_config(current: Settings | None = None) -> list[str]:
    """Return the list of production misconfigurations found.

    Returns rather than raises so the caller decides: :func:`app.main` logs
    every problem and then refuses to start in production, which is more useful
    than dying on whichever one happened to be checked first.
    """
    s = current or settings
    problems: list[str] = []

    if not s.is_production:
        return problems

    if s.jwt_secret == DEV_JWT_SECRET:
        problems.append("JWT_SECRET is still the development default")
    if len(s.jwt_secret) < 32:
        problems.append("JWT_SECRET is shorter than 32 characters")
    if s.debug:
        problems.append("DEBUG is enabled in production")
    if s.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL points at SQLite in production")
    if "complipilot:complipilot@" in s.database_url:
        problems.append("DATABASE_URL still carries the development password")
    if not s.require_totp_for_privileged_roles:
        problems.append(
            "REQUIRE_TOTP_FOR_PRIVILEGED_ROLES is off; section 8.2 makes it mandatory"
        )
    if any(o.startswith("http://") and "localhost" not in o for o in s.cors_origins):
        problems.append("BACKEND_CORS_ORIGINS contains a plaintext non-local origin")
    if not s.rate_limit_enabled:
        problems.append("RATE_LIMIT_ENABLED is off in production")

    return problems
