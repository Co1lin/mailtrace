"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from mailtrace.config import Settings
from mailtrace.store import Store


@pytest.fixture
def settings() -> Settings:
    return Settings(
        barcode_id=0,
        service_type_id=40,
        mailer_id=314159,
        usps_client_id="test-id",
        usps_client_secret="test-secret",
        bcg_username="user",
        bcg_password="pass",
        redis_url="redis://localhost:6379/0",
        session_secret="test-session-secret",
        # Open the feed for tests that aren't specifically about the allowlist.
        # Allowlist behavior has its own dedicated tests.
        feed_open=True,
    )


@pytest_asyncio.fixture
async def store() -> AsyncIterator[Store]:
    redis = FakeAsyncRedis()
    s = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    try:
        yield s
    finally:
        await redis.aclose()


class FakeUSPS:
    """Stub USPSClient: drives route behavior without hitting the network."""

    def __init__(self) -> None:
        self.tracking_payloads: dict[str, dict[str, Any]] = {}
        self.standardize_response: dict[str, str] | None = None
        self.tracking_error: Exception | None = None

    async def get_piece_tracking(self, imb: str) -> dict[str, Any]:
        if self.tracking_error is not None:
            raise self.tracking_error
        return self.tracking_payloads.get(imb, {"data": {"imb": imb, "scans": []}})

    async def standardize_address(self, address: dict[str, str]) -> Any:
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

    async def aclose(self) -> None:
        pass


@pytest.fixture
def fake_usps() -> FakeUSPS:
    return FakeUSPS()
