"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AIProviderName = Literal["mock", "claude", "grok"]


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
