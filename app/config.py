"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AIProviderName = Literal["mock", "claude", "grok"]
# Public on purpose: shown on the demo login page. Demo accounts only exist in DEMO_MODE
# (fake data, reseeded on every start).
DEFAULT_DEMO_PASSWORD = "copperline-demo"  # noqa: S105  # pragma: allowlist secret
DEMO_ROLES = ("admin", "rep", "viewer")


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

    # Field-level encryption of phone/email (see app/security/fields.py). Fernet keys,
    # comma-separated, newest first. Required in prod; dev/test fall back to a public
    # development key (and /readyz warns).
    field_encryption_key: SecretStr | None = None
    # HMAC key for the blind index used to look up encrypted phones/emails.
    blind_index_key: SecretStr | None = None

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
    send_windows: str = "mon-sun 08:00-20:00"
    # "One voice": at most this many touches per person in a rolling 7 days. The instant
    # SMS + email acknowledgment counts as one touch.
    max_touches_per_week: int = Field(default=3, ge=1)
    min_touch_gap_hours: float = Field(default=24.0, ge=0)
    callback_minutes: int = Field(default=15, ge=1, le=240)
    sends_paused: bool = False  # env kill switch; the DB flag can also pause
    kill_switch_recheck_seconds: float = Field(default=300.0, gt=0)

    # Dashboard logins (users + roles). Sessions are server-side; the cookie is signed.
    session_ttl_hours: float = Field(default=12.0, gt=0, le=24 * 7)  # absolute limit
    session_idle_minutes: float = Field(default=30.0, gt=0, le=24 * 60)
    login_max_failures: int = Field(default=5, ge=1, le=100)  # then the account locks
    login_lockout_base_seconds: float = Field(default=60.0, gt=0)  # doubles per extra failure
    login_lockout_max_seconds: float = Field(default=3600.0, gt=0)
    login_rate_limit_per_minute: float = Field(default=10.0, gt=0)  # per client IP
    # Two-factor (TOTP): required for admins; optional for reps; not for viewers.
    admin_2fa_required: bool = True
    totp_issuer: str = "speed-to-lead"

    # Data retention: leads older than this have their personal data removed by
    # `python -m app.cli retention` (anonymize keeps the row for metrics; delete removes it).
    retention_days: int = Field(default=365, ge=1, le=3650)
    retention_mode: Literal["anonymize", "delete"] = "anonymize"

    # Browser security. CORS is off unless origins are listed (comma-separated, exact).
    cors_allowed_origins: str = ""
    max_request_bytes: int = Field(default=64 * 1024, ge=1024, le=10 * 1024 * 1024)
    # Send HSTS on HTTPS responses.
    hsts_enabled: bool = True
    # /docs (Swagger UI) and /openapi.json. Turn off to hide the API description.
    api_docs_enabled: bool = True

    # Demo mode: reset + seed a SQLite DB with fake data on start, and run the worker and a
    # lead simulator inside the web process (one-service hosting). Never touches Postgres.
    demo_mode: bool = False
    # Demo accounts (<role>@example.com) seeded in demo mode, all with this password, shown
    # on the login page. For a public deployment, list only "viewer" in DEMO_ROLES.
    demo_password: SecretStr | None = None
    demo_roles: str = ",".join(DEMO_ROLES)
    # A demo uses the MOCK scorer unless this is set: a public demo should never spend a
    # real API key on whatever visitors type into the form.
    demo_allow_real_ai: bool = False
    demo_seed: int = 4242
    demo_leads: int = Field(default=150, ge=0, le=5000)
    demo_interval_min_seconds: float = Field(default=5.0, gt=0)
    demo_interval_max_seconds: float = Field(default=10.0, gt=0)
    # Where the simulator posts (the app itself). None -> http://127.0.0.1:$PORT (8000).
    demo_base_url: str | None = None

    # CRM sync: mock (default, in-process, no network) | hubspot (skeleton) | none
    # CRM sync: which CRM (hubspot | gohighlevel | salesforce | none), and whether to use its
    # in-memory MOCK (default) or the real API skeleton. "mock" means hubspot + CRM_MOCK.
    crm_provider: Literal["hubspot", "gohighlevel", "salesforce", "none", "mock"] = "hubspot"
    crm_mock: bool = True
    hubspot_token: SecretStr | None = None
    ghl_token: SecretStr | None = None
    ghl_location_id: str | None = None
    ghl_pipeline_id: str | None = None
    # "new=<stage id>,qualified=<id>,contacted=<id>,won=<id>,lost=<id>"
    ghl_stage_ids: str = ""
    salesforce_instance_url: str | None = None  # https://<my-domain>.my.salesforce.com
    salesforce_access_token: SecretStr | None = None
    salesforce_api_version: str = Field(default="v61.0", pattern=r"^v\d{2,3}\.0$")

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
    def _check_outbound_urls(self) -> Settings:
        from app.security.outbound import OutboundURLError, check_url

        for name in ("anthropic_base_url", "xai_base_url", "salesforce_instance_url"):
            value = getattr(self, name)
            if name == "salesforce_instance_url" and not (value or "").strip():
                self.salesforce_instance_url = None  # empty in .env means "not set"
                continue
            try:
                setattr(self, name, check_url(value))
            except OutboundURLError as exc:
                raise ValueError(f"{name.upper()}: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _check_cors(self) -> Settings:
        from urllib.parse import urlsplit

        for origin in self.cors_origins:
            parts = urlsplit(origin)
            if (
                origin == "*"
                or parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.path
                or parts.query
            ):
                raise ValueError(
                    f"CORS_ALLOWED_ORIGINS entries must be exact origins like "
                    f"https://app.example.com (got {origin!r})"
                )
        return self

    @model_validator(mode="after")
    def _check_demo(self) -> Settings:
        if not self.demo_mode:
            return self
        if not self.database_url.startswith("sqlite"):
            raise ValueError("DEMO_MODE resets the database, so it only runs on SQLite")
        if self.ai_provider != "mock" and not self.demo_allow_real_ai:
            raise ValueError(
                "DEMO_MODE uses AI_PROVIDER=mock; set DEMO_ALLOW_REAL_AI=true to use a real one"
            )
        if self.crm_provider not in ("none", "mock") and not self.crm_mock:
            raise ValueError("DEMO_MODE only syncs to MOCK CRMs (set CRM_MOCK=true)")
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
        if self.demo_password is None or not self.demo_password.get_secret_value():
            self.demo_password = SecretStr(DEFAULT_DEMO_PASSWORD)
        # The demo accounts are shared and public: keep a lockout short, so nobody can keep
        # them locked for long by failing on purpose.
        self.login_lockout_max_seconds = min(self.login_lockout_max_seconds, 60.0)
        self.login_lockout_base_seconds = min(self.login_lockout_base_seconds, 60.0)
        if not self.demo_role_list:
            raise ValueError(f"DEMO_ROLES must list some of {', '.join(DEMO_ROLES)}")
        return self

    @model_validator(mode="after")
    def _require_secret_in_prod(self) -> Settings:
        if self.secret_key is None or not self.secret_key.get_secret_value():
            if self.environment == "prod":
                raise ValueError("SECRET_KEY must be set when ENVIRONMENT=prod")
            self.secret_key = SecretStr(secrets.token_urlsafe(32))
        return self

    @model_validator(mode="after")
    def _check_field_keys(self) -> Settings:
        from cryptography.fernet import Fernet

        from app.security.fields import dev_key, parse_keys

        fallbacks: dict[str, Callable[[], str]] = {
            "field_encryption_key": lambda: dev_key("field-encryption"),
            "blind_index_key": lambda: dev_key("blind-index"),
        }
        if self.demo_mode:  # fake data, reseeded on every start: random keys are fine
            fallbacks = {
                "field_encryption_key": lambda: Fernet.generate_key().decode(),
                "blind_index_key": lambda: secrets.token_urlsafe(32),
            }
        for name, fallback in fallbacks.items():
            value = getattr(self, name)
            if value is None or not value.get_secret_value().strip():
                if self.environment == "prod":
                    raise ValueError(f"{name.upper()} must be set when ENVIRONMENT=prod")
                setattr(self, name, SecretStr(fallback()))
        assert self.field_encryption_key is not None
        assert self.blind_index_key is not None
        try:
            parse_keys(self.field_encryption_key.get_secret_value())
        except ValueError as exc:
            raise ValueError(
                "FIELD_ENCRYPTION_KEY must be comma-separated Fernet keys (generate one with "
                "python -m app.cli generate-key)"
            ) from exc
        if len(self.blind_index_key.get_secret_value()) < 16:
            raise ValueError("BLIND_INDEX_KEY must be at least 16 characters")
        return self

    @property
    def demo_role_list(self) -> list[str]:
        wanted = [r.strip().lower() for r in self.demo_roles.split(",") if r.strip()]
        return [r for r in DEMO_ROLES if r in wanted]

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def uses_dev_field_keys(self) -> bool:
        from app.security.fields import dev_key

        assert self.field_encryption_key is not None
        assert self.blind_index_key is not None
        return dev_key("field-encryption") in self.field_encryption_key.get_secret_value() or (
            self.blind_index_key.get_secret_value() == dev_key("blind-index")
        )

    @property
    def secret_key_bytes(self) -> bytes:
        assert self.secret_key is not None  # guaranteed by the validator
        return self.secret_key.get_secret_value().encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
