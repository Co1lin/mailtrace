"""Route-level tests. We build a FastAPI app with a fake Redis and a fake
USPS client so the routes can be exercised without external services.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from mailtrace.app import STATIC_DIR, TEMPLATES_DIR, imb_font_data_uri
from mailtrace.config import Settings
from mailtrace.routes import router
from mailtrace.store import Store


def _build_app(settings: Settings, store: Store, usps: Any) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test", same_site="lax")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["imb_font_data_uri"] = imb_font_data_uri()
    app.state.settings = settings
    app.state.store = store
    app.state.usps = usps
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def client(settings: Settings, fake_usps: Any) -> AsyncIterator[AsyncClient]:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await redis.aclose()


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_index_renders(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Generate envelope" in resp.text


async def test_generate_then_download_html(client: AsyncClient) -> None:
    resp = await client.post(
        "/generate",
        data={
            "sender_address": "Alice\n100 Main St\nAnywhere, CA 90000",
            "recipient_name": "Bob",
            "recipient_company": "",
            "recipient_street": "200 Market St",
            "recipient_address2": "",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
        },
    )
    assert resp.status_code == 200
    assert "Receipt ZIP" in resp.text
    # session cookie should let us hit /download
    dl = await client.get("/download/envelope/html")
    assert dl.status_code == 200
    assert "USPSIMBStandard" in dl.text


async def test_generate_rejects_bad_zip(client: AsyncClient) -> None:
    resp = await client.post(
        "/generate",
        data={
            "sender_address": "x",
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "1234",
        },
    )
    assert resp.status_code == 400


async def test_track_merges_pushed_events(
    client: AsyncClient, fake_usps: Any, settings: Settings
) -> None:
    # Push an event via the feed endpoint, then read it back via /api/track.
    raw_imb = (
        f"{settings.barcode_id:02d}{settings.service_type_id:03d}"
        f"{settings.mailer_id:d}{1:06d}{'94105'}"
    )
    feed = await client.post(
        "/usps_feed",
        json={
            "events": [
                {
                    "imb": raw_imb,
                    "handlingEventType": "L",
                    "scanDatetime": "2024-06-01T10:00:00",
                    "scanEventCode": "SL",
                    "scanFacilityCity": "San Francisco",
                    "scanFacilityState": "CA",
                }
            ]
        },
    )
    assert feed.status_code == 200
    assert feed.json() == {"stored": 1}

    fake_usps.tracking_payloads[raw_imb] = {
        "data": {"imb": raw_imb, "scans": [], "expected_delivery_date": "2024-06-03"}
    }
    track = await client.get("/api/track?serial=1&receipt_zip=94105")
    assert track.status_code == 200
    body = track.json()
    assert body["error"] is None
    scans = body["data"]["scans"]
    assert len(scans) == 1
    assert scans[0]["scan_event_code"] == "SL"


async def test_feed_drops_non_letter_events(client: AsyncClient) -> None:
    resp = await client.post(
        "/usps_feed",
        json={
            "events": [
                {"imb": "x", "handlingEventType": "X"},  # ignored
                {"handlingEventType": "L"},  # missing imb
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"stored": 0}


async def test_feed_rejects_untrusted_caller(settings: Settings, fake_usps: Any) -> None:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    settings = settings.model_copy(update={"trusted_feed_ips": ["10.0.0.1"]})
    app = _build_app(settings, store, fake_usps)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/usps_feed", json={"events": []})
        assert resp.status_code == 403
    await redis.aclose()


async def test_track_falls_back_when_usps_errors(client: AsyncClient, fake_usps: Any) -> None:
    from mailtrace.usps import USPSError

    fake_usps.tracking_error = USPSError("upstream down")
    resp = await client.get("/api/track?serial=1&receipt_zip=94105")
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] == "upstream down"
    assert body["data"]["scans"] == []
