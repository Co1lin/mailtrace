"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mailtrace import auth as auth_lib
from mailtrace.config import Settings
from mailtrace.db import make_engine, make_sessionmaker
from mailtrace.models import Base, User
from mailtrace.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        usps_client_id="test-id",
        usps_client_secret="test-secret",
        bcg_username="user",
        bcg_password="pass",
        redis_url="redis://localhost:6379/0",
        session_secret="test-session-secret",
        # SQLite per-test in a temp file so each test is isolated.
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        # Per-test archive dir (most feed tests run with archiving off).
        ingest_archive_dir=str(tmp_path / "ingest_raw"),
    )


@pytest_asyncio.fixture
async def store() -> AsyncIterator[Store]:
    redis = FakeAsyncRedis()
    s = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    try:
        yield s
    finally:
        await redis.aclose()


@pytest_asyncio.fixture
async def db_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(
    db_engine: AsyncEngine,
) -> async_sessionmaker:
    return make_sessionmaker(db_engine)


@pytest_asyncio.fixture
async def regular_user(db_sessionmaker: async_sessionmaker) -> User:
    async with db_sessionmaker() as db:
        user = User(
            email="user@example.com",
            password_hash=auth_lib.hash_password("user-password-12345"),
            is_admin=False,
            is_active=True,
            must_change_password=False,
            mailer_id=314159,
            barcode_id=0,
            service_type_id=40,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest_asyncio.fixture
async def admin_user(db_sessionmaker: async_sessionmaker) -> User:
    async with db_sessionmaker() as db:
        user = User(
            email="admin@example.com",
            password_hash=auth_lib.hash_password("admin-password-12345"),
            is_admin=True,
            is_active=True,
            must_change_password=False,
            mailer_id=900000001,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


class FakeUSPS:
    """Stub USPSClient: drives route behavior without hitting the network.

    Mirrors USPSClient's per-user-takes-User method signatures (every
    method takes the calling User as its first positional argument)."""

    def __init__(self) -> None:
        self.tracking_payloads: dict[str, dict[str, Any]] = {}
        self.standardize_response: dict[str, str] | None = None
        self.tracking_error: Exception | None = None

    async def get_piece_tracking(self, user: Any, imb: str) -> dict[str, Any]:
        if self.tracking_error is not None:
            raise self.tracking_error
        return self.tracking_payloads.get(imb, {"data": {"imb": imb, "scans": []}})

    async def standardize_address(self, user: Any, address: dict[str, str]) -> Any:
        from mailtrace.usps import StandardizedAddress

        if self.standardize_response is None:
            return StandardizedAddress(
                firmname="",
                street_address=address.get("street_address", ""),
                address2="",
                city=address.get("city", ""),
                state=address.get("state", ""),
                zip5=address.get("zip5", ""),
                zip4="0001",
                dp="00",
            )
        return StandardizedAddress(**self.standardize_response)

    async def probe_modern_creds(self, user: Any) -> None:
        if self.tracking_error is not None:
            raise self.tracking_error

    async def probe_legacy_creds(self, user: Any) -> None:
        if self.tracking_error is not None:
            raise self.tracking_error

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_usps() -> FakeUSPS:
    return FakeUSPS()
