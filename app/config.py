"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AIProviderName = Literal["mock", "claude", "grok"]
# Public on purpose: shown on the demo login page, only valid in DEMO_MODE (fake data).
DEFAULT_DEMO_TOKEN = "copperline-demo"  # noqa: S105  # pragma: allowlist secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "speed-to-lead"
    company_name: str = "Copperline Roofing & Solar (fictional)"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "sqlite:///./speed_to_lead.db"

    # Signs CSRF tokens. In dev/test a random per-process key is generated when unset.
    secret_key: SecretStr | None = None

    meta_verify_token: SecretStr | None = None
    meta_app_secret: SecretStr | None = None

    rate_limit_capacity: int = Field(default=10, ge=1)
    rate_limit_refill_per_minute: float = Field(default=10.0, gt=0)
    webhook_rate_limit_capacity: int = Field(default=120, ge=1)
    webhook_rate_limit_refill_per_minute: float = Field(default=600.0, gt=0)
    phone_daily_cap: int = Field(default=3, ge=1)
    trust_forwarded_for: bool = False

    ai_provider: AIProviderName = "mock"
    ai_timeout_seconds: float = Field(default=4.0, gt=0, le=30)
    ai_max_attempts: int = Field(default=3, ge=1, le=3)
    ai_total_budget_seconds: float = Field(default=8.0, gt=0, le=60)
    ai_min_confidence: float = Field(default=0.5, ge=0, le=1)
    ai_breaker_threshold: int = Field(default=5, ge=1)
    ai_breaker_cooldown_seconds: float = Field(default=60.0, gt=0)

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-haiku-5-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    xai_api_key: SecretStr | None = None
    xai_model: str = "grok-4"
    xai_base_url: str = "https://api.x.ai"

    # Operator API (/admin/*, pipeline moves, tasks, lead detail). Unset -> those endpoints
    # fail closed with 503.
    admin_token: SecretStr | None = None
    # Shared secret for the (mock) inbound SMS reply webhook. Unset -> 503.
    inbound_webhook_secret: SecretStr | None = None

    # Job queue / worker
    job_max_attempts: int = Field(default=5, ge=1, le=20)
    job_lease_seconds: float = Field(default=60.0, gt=0)
    job_backoff_base_seconds: float = Field(default=5.0, gt=0)
    job_backoff_cap_seconds: float = Field(default=900.0, gt=0)
    worker_poll_seconds: float = Field(default=1.0, gt=0)

    # Follow-up sequence and send guards
    followup_sequence_path: str | None = None  # YAML; None -> the bundled default sequence
    business_timezone: str = "America/New_York"
    # Enforce the sending window below (lead-local time; business timezone if unknown).
    quiet_hours_enabled: bool = True
    send_windows: str = "mon-sat 09:00-20:00; sun 15:00-18:00"
    # "One voice": at most this many touches per person in a rolling 7 days. The instant
    # SMS + email acknowledgment counts as one touch.
    max_touches_per_week: int = Field(default=3, ge=1)
    min_touch_gap_hours: float = Field(default=24.0, ge=0)
    callback_minutes: int = Field(default=15, ge=1, le=240)
    sends_paused: bool = False  # env kill switch; the DB flag can also pause
    kill_switch_recheck_seconds: float = Field(default=300.0, gt=0)

    # Dashboard login (temporary until real users and roles): a signed session cookie.
    session_ttl_hours: float = Field(default=12.0, gt=0, le=24 * 7)

    # Demo mode: reset + seed a SQLite DB with fake data on start, and run the worker and a
    # lead simulator inside the web process (one-service hosting). Never touches Postgres.
    demo_mode: bool = False
    # Login token for the demo dashboard, shown on the login page in demo mode. It only
    # opens the dashboard (fake data), never the bearer-token operator API.
    demo_token: SecretStr | None = None
    demo_seed: int = 4242
    demo_leads: int = Field(default=150, ge=0, le=5000)
    demo_interval_min_seconds: float = Field(default=5.0, gt=0)
    demo_interval_max_seconds: float = Field(default=10.0, gt=0)
    # Where the simulator posts (the app itself). None -> http://127.0.0.1:$PORT (8000).
    demo_base_url: str | None = None

    # CRM sync: mock (default, in-process, no network) | hubspot (skeleton) | none
    crm_provider: Literal["mock", "hubspot", "none"] = "mock"
    hubspot_token: SecretStr | None = None

    @model_validator(mode="after")
    def _check_timezone(self) -> Settings:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.business_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown BUSINESS_TIMEZONE: {self.business_timezone!r}") from exc
        from app.followups.guards import SendWindow

        try:
            SendWindow.parse(self.send_windows)
        except ValueError as exc:
            raise ValueError(f"invalid SEND_WINDOWS: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_demo(self) -> Settings:
        if not self.demo_mode:
            return self
        if not self.database_url.startswith("sqlite"):
            raise ValueError("DEMO_MODE resets the database, so it only runs on SQLite")
        if self.demo_interval_min_seconds > self.demo_interval_max_seconds:
            raise ValueError("DEMO_INTERVAL_MIN_SECONDS must be <= the max")
        # The demo talks to itself through the real, authenticated endpoints. Secrets that
        # were not provided are random per process; nothing here is a real credential.
        for name in (
            "meta_app_secret",
            "meta_verify_token",
            "inbound_webhook_secret",
            "admin_token",
        ):
            value = getattr(self, name)
            if value is None or not value.get_secret_value():
                setattr(self, name, SecretStr(secrets.token_urlsafe(24)))
        if self.demo_token is None or not self.demo_token.get_secret_value():
            self.demo_token = SecretStr(DEFAULT_DEMO_TOKEN)
        return self

    @model_validator(mode="after")
    def _require_secret_in_prod(self) -> Settings:
        if self.secret_key is None or not self.secret_key.get_secret_value():
            if self.environment == "prod":
                raise ValueError("SECRET_KEY must be set when ENVIRONMENT=prod")
            self.secret_key = SecretStr(secrets.token_urlsafe(32))
        return self

    @property
    def secret_key_bytes(self) -> bytes:
        assert self.secret_key is not None  # guaranteed by the validator
        return self.secret_key.get_secret_value().encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
