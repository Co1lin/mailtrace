"""Tests for the USPS client's response handling.

These focus on the failure modes that previously escaped as uncaught
exceptions (and surfaced as HTTP 500s on the per-piece refresh route):
USPS answering 200 with an empty or non-JSON body.
"""

from __future__ import annotations

import httpx
import pytest

from mailtrace.models import User
from mailtrace.store import Store
from mailtrace.usps import USPSClient, USPSError


def _user() -> User:
    return User(
        id=1,
        email="u@example.com",
        password_hash="x",
        mailer_id=314159,
        bcg_username="user",
        bcg_password="pass",
    )


async def _client_with(store: Store, handler) -> USPSClient:  # type: ignore[no-untyped-def]
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = USPSClient(store=store, http_client=http)
    # Pre-seed a valid legacy token so get_piece_tracking skips the OAuth hop.
    token_key, type_key, expiry_key = (
        "mailtrace:usps:user:1:iv:token",
        "mailtrace:usps:user:1:iv:type",
        "mailtrace:usps:user:1:iv:expiry",
    )
    await store.set_str(token_key, "tok", ttl_seconds=3600)
    await store.set_str(type_key, "Bearer", ttl_seconds=3600)
    await store.set_str(expiry_key, str(2_000_000_000.0), ttl_seconds=3600)
    return client


async def test_get_piece_tracking_empty_body_returns_empty(store: Store) -> None:
    """A 200 with an empty body means 'no tracking data' — not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = await _client_with(store, handler)
    result = await client.get_piece_tracking(_user(), "0004031415900000194105")
    assert result == {}
    await client.aclose()


async def test_get_piece_tracking_non_json_body_raises_uspserror(store: Store) -> None:
    """A 200 with a non-empty, non-JSON body is a USPSError, not a 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    client = await _client_with(store, handler)
    with pytest.raises(USPSError):
        await client.get_piece_tracking(_user(), "0004031415900000194105")
    await client.aclose()
