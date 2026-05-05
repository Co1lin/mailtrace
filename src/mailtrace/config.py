"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config is provided through `MAILTRACE_*` env vars.

    See `.env.example` for the full list. Defaults are chosen so the
    application starts (with a clear error from USPS) even before
    credentials are configured, which simplifies smoke-testing in Docker.
    """

    model_config = SettingsConfigDict(
        env_prefix="MAILTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IMb identity (issued by USPS Business Customer Gateway)
    barcode_id: int = Field(0, ge=0, le=99)
    service_type_id: int = Field(40, ge=0, le=999)
    mailer_id: int = Field(0, description="6- or 9-digit USPS Mailer ID")

    # USPS API credentials (apis.usps.com)
    usps_client_id: SecretStr = SecretStr("")
    usps_client_secret: SecretStr = SecretStr("")

    # Business Customer Gateway (legacy IV oauth)
    bcg_username: SecretStr = SecretStr("")
    bcg_password: SecretStr = SecretStr("")

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Web app
    session_secret: SecretStr = SecretStr("change-me")
    root_path: str = ""
    trusted_feed_ips: list[str] = Field(default_factory=list)

    # Operational
    serial_rolling_window: int = 50
    feed_event_ttl_seconds: int = 60 * 24 * 60 * 60  # 60 days
    timezone: str = "UTC"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from env at construction
