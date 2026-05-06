"""Bootstrap configuration loaded from environment variables / .env.

This module covers ONLY the values that must be available before the app
can start at all (DB / Redis URLs, session secret, host/port, infra paths).
Everything else — USPS credentials, poll cadence, etc. — lives in the
`AppConfig` DB singleton and is editable from /admin/settings without a
restart. See `models.AppConfig` for what moved.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bootstrap-time config from `MAILTRACE_*` env vars.

    See `.env.example` for the full list and `/admin/settings` for the
    runtime config (USPS API creds, poll cadence, …) that lives in the DB.
    """

    model_config = SettingsConfigDict(
        env_prefix="MAILTRACE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- truly required at boot ----
    # Signs the session cookie. Without it the app can't authenticate the
    # admin who would log in to set anything else, so this is the one
    # value that genuinely must come from env.
    session_secret: SecretStr = SecretStr("change-me")

    # Persistent storage. SQLite by default at the absolute path /data,
    # which is where docker-compose.yml mounts the named volume and where
    # the Dockerfile chowns to uid 10001. Use sqlite+aiosqlite for the
    # async driver. For Postgres: postgresql+asyncpg://...
    #
    # Format note: SQLAlchemy distinguishes 3-slash (relative) from
    # 4-slash (absolute) sqlite URLs. The 4-slash form below points at
    # /data/mailtrace.db; a 3-slash form would resolve relative to the
    # container's WORKDIR (/app), which is not where the volume mounts.
    database_url: str = "sqlite+aiosqlite:////data/mailtrace.db"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Mount the app under a subpath (FastAPI applies this at construction
    # time, so it has to be set before the lifespan runs).
    root_path: str = ""

    # ---- infrastructure (boot time, rarely changed) ----
    # Where /usps_feed archives raw POSTed JSON for audit / replay.
    ingest_archive_dir: str = "./data/ingest_raw"
    # Caddy / front-of-LB IP(s). When the immediate peer is in this list,
    # X-Forwarded-For is honored for source-IP logging on /usps_feed.
    # Pure operational; auth is Basic Auth.
    trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Store-level tuning (Redis serial allocator). Almost never changed.
    serial_rolling_window: int = 50
    feed_event_ttl_seconds: int = 60 * 24 * 60 * 60  # 60 days


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
