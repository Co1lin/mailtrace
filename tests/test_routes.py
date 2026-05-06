"""Route-level tests against the full FastAPI app (auth, DB, middleware)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from mailtrace.app import STATIC_DIR, TEMPLATES_DIR, imb_font_data_uri
from mailtrace.config import Settings
from mailtrace.middleware import AuthMiddleware
from mailtrace.models import MailPiece, User
from mailtrace.routes import router
from mailtrace.store import Store


def select_all_user_pieces(user_id: int):  # type: ignore[no-untyped-def]
    return select(MailPiece).where(MailPiece.user_id == user_id).order_by(MailPiece.id)


def _build_app(
    settings: Settings,
    store: Store,
    usps: Any,
    sm: async_sessionmaker,
) -> FastAPI:
    app = FastAPI()
    # Order matters: AuthMiddleware added first runs after SessionMiddleware,
    # which is what we want (SessionMiddleware populates request.session).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="test", same_site="lax")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["imb_font_data_uri"] = imb_font_data_uri()
    app.state.settings = settings
    app.state.store = store
    app.state.usps = usps
    app.state.templates = templates
    app.state.db_sessionmaker = sm
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/auth/login",
        data={"email": email, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest_asyncio.fixture
async def anon_client(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,  # ensures the "first-run setup" gate is past
) -> AsyncIterator[AsyncClient]:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await redis.aclose()


@pytest_asyncio.fixture
async def empty_db_client(
    settings: Settings, fake_usps: Any, db_sessionmaker: async_sessionmaker
) -> AsyncIterator[AsyncClient]:
    """Fresh DB with no users — exercises the first-run setup flow."""
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await redis.aclose()


@pytest_asyncio.fixture
async def client(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> AsyncIterator[AsyncClient]:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, regular_user.email, "user-password-12345")
        yield ac
    await redis.aclose()


@pytest_asyncio.fixture
async def admin_client(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    admin_user: User,
) -> AsyncIterator[AsyncClient]:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, admin_user.email, "admin-password-12345")
        yield ac
    await redis.aclose()


# ---------------------------------------------------------------------------
# Public + auth flow
# ---------------------------------------------------------------------------


async def test_root_redirects_to_login_when_anonymous(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["location"]


async def test_favicon_ico_redirects_to_svg_without_auth(anon_client: AsyncClient) -> None:
    """Browsers fetch /favicon.ico unconditionally on first visit. The
    auth middleware must NOT bounce that request through /auth/login,
    and the redirect to the actual SVG must work without a session."""
    resp = await anon_client.get("/favicon.ico", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/static/favicon.svg"


async def test_favicon_svg_serves_unauthenticated(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/static/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg")
    assert b"<svg" in resp.content


async def test_login_form_renders(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/auth/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


async def test_login_with_bad_credentials_fails(
    anon_client: AsyncClient, regular_user: User
) -> None:
    resp = await anon_client.post(
        "/auth/login",
        data={"email": regular_user.email, "password": "wrong", "next": "/"},
    )
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.text


async def test_login_logout_round_trip(client: AsyncClient) -> None:
    home = await client.get("/")
    assert home.status_code == 200
    assert "Welcome" in home.text

    out = await client.post("/auth/logout", follow_redirects=False)
    assert out.status_code == 303
    again = await client.get("/", follow_redirects=False)
    assert again.status_code == 302


async def test_must_change_password_redirects(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
) -> None:
    from mailtrace import auth as auth_lib

    async with db_sessionmaker() as db:
        u = User(
            email="fresh@example.com",
            password_hash=auth_lib.hash_password("temp-password-12345"),
            must_change_password=True,
            mailer_id=314159,
        )
        db.add(u)
        await db.commit()

    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, "fresh@example.com", "temp-password-12345")
        # Visiting any non-bypass URL should redirect to /auth/change-password.
        resp = await ac.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/auth/change-password")
        # Now actually change the password.
        ok = await ac.post(
            "/auth/change-password",
            data={
                "current_password": "temp-password-12345",
                "new_password": "brand-new-password",
                "confirm_password": "brand-new-password",
            },
            follow_redirects=False,
        )
        assert ok.status_code == 303
        # And the redirect lock is gone.
        home = await ac.get("/", follow_redirects=False)
        assert home.status_code == 200
    await redis.aclose()


# ---------------------------------------------------------------------------
# Mail pieces
# ---------------------------------------------------------------------------


async def test_create_piece_inline_then_download(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await client.post(
        "/pieces/new",
        data={
            "label": "rent check",
            "sender_address_id": "",
            "sender_block_inline": "Alice\n100 Main St",
            "recipient_address_id": "",
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith("/pieces/")
    piece_id = int(location.rsplit("/", 1)[-1])

    detail = await client.get(f"/pieces/{piece_id}")
    assert detail.status_code == 200
    assert "rent check" in detail.text

    dl = await client.get(f"/pieces/{piece_id}/download/envelope.html")
    assert dl.status_code == 200
    assert "USPSIMBStandard" in dl.text

    from mailtrace.models import MailPiece

    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, piece_id)
        assert piece is not None
        assert piece.recipient_zip_raw == "94105"
        assert piece.include_zip_in_imb is True
        assert piece.imb_raw.endswith("94105")  # zip included
        assert piece.label == "rent check"


async def test_create_piece_zipless_imb(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            # Note: do NOT send include_zip_in_imb checkbox
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    piece_id = int(resp.headers["location"].rsplit("/", 1)[-1])

    from mailtrace.models import MailPiece

    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, piece_id)
        assert piece is not None
        assert piece.include_zip_in_imb is False
        assert not piece.imb_raw.endswith("94105")
        # The address block on the envelope still has the ZIP for routing.
        assert "94105" in piece.recipient_block


async def test_create_piece_blocked_when_no_mailer_id(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
) -> None:
    from mailtrace import auth as auth_lib

    async with db_sessionmaker() as db:
        u = User(
            email="nomid@example.com",
            password_hash=auth_lib.hash_password("nomid-password-1234"),
            must_change_password=False,
            mailer_id=None,
        )
        db.add(u)
        await db.commit()

    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, "nomid@example.com", "nomid-password-1234")
        resp = await ac.post(
            "/pieces/new",
            data={
                "recipient_name": "Bob",
                "recipient_street": "200 Market St",
                "recipient_city": "Sometown",
                "recipient_state": "CA",
                "recipient_zip": "94105",
            },
        )
        assert resp.status_code == 400
        assert "Mailer ID" in resp.text
    await redis.aclose()


async def test_batch_create_from_address_book(
    client: AsyncClient, regular_user: User, db_sessionmaker: async_sessionmaker
) -> None:
    # Seed two recipient addresses for the user.
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a1 = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        a2 = Address(user_id=regular_user.id, label="bob", role="recipient", zip="10001")
        db.add_all([a1, a2])
        await db.commit()
        await db.refresh(a1)
        await db.refresh(a2)
        a1_id, a2_id = a1.id, a2.id

    resp = await client.post(
        "/pieces/batch",
        data={
            "row-0-label": "first",
            "row-0-recipient_id": str(a1_id),
            "row-0-include_zip": "on",
            "row-1-label": "second",
            "row-1-recipient_id": str(a2_id),
            "row-1-include_zip": "on",
            # row 2 is blank → skipped silently
            "row-2-label": "",
            "row-2-recipient_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        labels = sorted(p.label for p in rows)
        assert labels == ["first", "second"]


async def test_archive_unarchive_delete(
    client: AsyncClient, regular_user: User, db_sessionmaker: async_sessionmaker
) -> None:
    # Create one piece via the UI.
    create = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
        },
        follow_redirects=False,
    )
    pid = int(create.headers["location"].rsplit("/", 1)[-1])

    arch = await client.post(f"/pieces/{pid}/archive", follow_redirects=False)
    assert arch.status_code == 303
    archived_list = await client.get("/pieces/?archived=1")
    assert f"/pieces/{pid}" in archived_list.text

    unarch = await client.post(f"/pieces/{pid}/unarchive", follow_redirects=False)
    assert unarch.status_code == 303

    delete = await client.post(f"/pieces/{pid}/delete", follow_redirects=False)
    assert delete.status_code == 303
    from mailtrace.models import MailPiece

    async with db_sessionmaker() as db:
        assert await db.get(MailPiece, pid) is None


async def test_pieces_isolated_per_user(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    admin_user: User,
) -> None:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, regular_user.email, "user-password-12345")
        create = await ac.post(
            "/pieces/new",
            data={
                "recipient_name": "Bob",
                "recipient_street": "200 Market St",
                "recipient_city": "Sometown",
                "recipient_state": "CA",
                "recipient_zip": "94105",
                "include_zip_in_imb": "true",
            },
            follow_redirects=False,
        )
        pid = int(create.headers["location"].rsplit("/", 1)[-1])

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, admin_user.email, "admin-password-12345")
        # Admin (different user) must not be able to see another user's piece.
        peek = await ac.get(f"/pieces/{pid}")
        assert peek.status_code == 404
        listing = await ac.get("/pieces/")
        assert f"/pieces/{pid}" not in listing.text
    await redis.aclose()


# ---------------------------------------------------------------------------
# Address book
# ---------------------------------------------------------------------------


async def test_address_crud(client: AsyncClient) -> None:
    create = await client.post(
        "/addresses/",
        data={
            "label": "home",
            "role": "recipient",
            "name": "Bob",
            "street": "200 Market St",
            "city": "Sometown",
            "state": "CA",
            "zip": "94105",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    listing = await client.get("/addresses/")
    assert listing.status_code == 200
    assert "home" in listing.text
    assert "Bob" in listing.text

    # Same label twice → 409
    dup = await client.post(
        "/addresses/",
        data={"label": "home", "role": "recipient", "name": "x", "zip": "94105"},
        follow_redirects=False,
    )
    assert dup.status_code == 409


async def test_addresses_isolated_per_user(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    admin_user: User,
) -> None:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, regular_user.email, "user-password-12345")
        await ac.post(
            "/addresses/",
            data={"label": "mine", "role": "recipient", "zip": "94105"},
            follow_redirects=False,
        )

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, admin_user.email, "admin-password-12345")
        listing = await ac.get("/addresses/")
        assert "mine" not in listing.text  # admin should not see user's address

    await redis.aclose()


# ---------------------------------------------------------------------------
# Admin portal
# ---------------------------------------------------------------------------


async def test_admin_portal_requires_admin(client: AsyncClient) -> None:
    resp = await client.get("/admin/", follow_redirects=False)
    assert resp.status_code == 403


async def test_admin_can_create_user_and_force_reset(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    create = await admin_client.post(
        "/admin/users",
        data={"email": "newbie@example.com", "mailer_id": "999"},
        follow_redirects=False,
    )
    assert create.status_code == 303

    page = await admin_client.get("/admin/")
    assert "newbie@example.com" in page.text
    assert "Temporary password" in page.text  # flash from session

    # Find the user's id, force-reset.
    from sqlalchemy import select

    from mailtrace.models import User as UserModel

    async with db_sessionmaker() as db:
        u = (
            await db.execute(select(UserModel).where(UserModel.email == "newbie@example.com"))
        ).scalar_one()
        old_hash = u.password_hash

    reset = await admin_client.post(f"/admin/users/{u.id}/force-reset", follow_redirects=False)
    assert reset.status_code == 303

    async with db_sessionmaker() as db:
        u2 = await db.get(UserModel, u.id)
        assert u2 is not None
        assert u2.must_change_password is True
        assert u2.password_hash != old_hash


async def test_admin_cannot_demote_last_admin(admin_client: AsyncClient, admin_user: User) -> None:
    resp = await admin_client.post(
        f"/admin/users/{admin_user.id}/toggle-admin", follow_redirects=False
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# IV-MTR push receiver (Basic Auth, gzip, archive, IngestLog)
# ---------------------------------------------------------------------------


_IV_USER = "iv_test"
_IV_PASS = "iv-test-password-very-long-1234567890"


def _iv_auth_header(user: str = _IV_USER, password: str = _IV_PASS) -> str:
    import base64

    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


async def _enable_ingest(
    db_sessionmaker: async_sessionmaker,
    *,
    user: str = _IV_USER,
    password: str = _IV_PASS,
    expect_gzip: bool = False,
    max_body_mb: int = 100,
    archive: bool = False,
) -> None:
    from mailtrace.models import IngestSubscription

    async with db_sessionmaker() as db:
        cfg = (
            await db.execute(select(IngestSubscription).where(IngestSubscription.id == 1))
        ).scalar_one_or_none()
        if cfg is None:
            cfg = IngestSubscription(id=1)
            db.add(cfg)
        cfg.enabled = True
        cfg.basic_auth_user = user
        cfg.basic_auth_pass = password
        cfg.expect_gzip = expect_gzip
        cfg.max_body_mb = max_body_mb
        cfg.archive_payloads = archive
        await db.commit()


async def test_feed_returns_503_when_subscription_disabled(anon_client: AsyncClient) -> None:
    """No row / not enabled → 503 (so USPS retries) rather than 404."""
    resp = await anon_client.post(
        "/usps_feed",
        json={"data": []},
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 503


async def test_feed_rejects_unauthenticated(
    anon_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await _enable_ingest(db_sessionmaker)
    no_auth = await anon_client.post("/usps_feed", json={"data": []})
    assert no_auth.status_code == 401
    assert no_auth.headers.get("www-authenticate", "").lower().startswith("basic")
    bad_pw = await anon_client.post(
        "/usps_feed",
        json={"data": []},
        headers={"authorization": _iv_auth_header(password="wrong")},
    )
    assert bad_pw.status_code == 401
    bad_user = await anon_client.post(
        "/usps_feed",
        json={"data": []},
        headers={"authorization": _iv_auth_header(user="wrong")},
    )
    assert bad_user.status_code == 401


async def test_feed_routes_event_to_owning_piece(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    await _enable_ingest(db_sessionmaker)
    create = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
        },
        follow_redirects=False,
    )
    pid = int(create.headers["location"].rsplit("/", 1)[-1])

    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        imb = piece.imb_raw

    # IV-style payload: top-level wrapper with `data` array; field names
    # exactly as documented in the IV portal.
    resp = await anon_client.post(
        "/usps_feed",
        json={
            "feedId": "12345",
            "fileGenerationDateTime": "2025-01-15T10:05:00Z",
            "recordCount": 1,
            "data": [
                {
                    "imb": imb,
                    "scanDateTime": "2025-01-15T10:00:00Z",
                    "scanEventCode": "SPM",
                    "handlingEventType": "Processed",
                    "scanFacilityName": "SAN FRANCISCO P&DC",
                    "scanFacilityCity": "San Francisco",
                    "scanFacilityState": "CA",
                    "scanFacilityZIP": "94110",
                }
            ],
        },
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"stored": 1, "matched": 1, "orphaned": 0, "records": 1}

    detail = await client.get(f"/pieces/{pid}")
    assert "San Francisco" in detail.text
    assert "feed" in detail.text  # source pill

    # Re-post same event → dedup (no extra row), under a different
    # field-name capitalization to prove the loose normalizer works.
    again = await anon_client.post(
        "/usps_feed",
        json={
            "data": [
                {
                    "IMB": imb,
                    "scan_datetime": "2025-01-15T10:00:00Z",  # snake_case variant
                    "scaneventcode": "SPM",
                    "scan_facility_zip": "94110",
                }
            ]
        },
        headers={"authorization": _iv_auth_header()},
    )
    assert again.json()["stored"] == 0
    assert again.json()["matched"] == 1


async def test_feed_drops_orphans_logs_them(
    anon_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await _enable_ingest(db_sessionmaker)
    resp = await anon_client.post(
        "/usps_feed",
        json={
            "data": [
                {"imb": "9999999999999999999999", "scanDateTime": "2025-01-01T00:00:00Z"},
                {"scanDateTime": "2025-01-01T00:00:00Z"},  # missing imb
            ]
        },
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 200
    assert resp.json() == {"stored": 0, "matched": 0, "orphaned": 2, "records": 2}


async def test_feed_accepts_records_wrapper_too(
    anon_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """Some IV deployments wrap events under "records" instead of "data"."""
    await _enable_ingest(db_sessionmaker)
    resp = await anon_client.post(
        "/usps_feed",
        json={"records": [{"imb": "no-match"}]},
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 200
    assert resp.json()["records"] == 1


async def test_feed_rejects_oversized_body(
    anon_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    await _enable_ingest(db_sessionmaker, max_body_mb=1)
    big = b'{"data": [' + b'{"imb":"x"},' * 100_000 + b'{"imb":"x"}]}'
    assert len(big) > 1024 * 1024
    resp = await anon_client.post(
        "/usps_feed",
        content=big,
        headers={
            "authorization": _iv_auth_header(),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 413


async def test_feed_decompresses_gzip(
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    import gzip
    import json as json_mod

    await _enable_ingest(db_sessionmaker)
    raw = json_mod.dumps({"data": [{"imb": "no-match"}]}).encode()
    gz = gzip.compress(raw)
    resp = await anon_client.post(
        "/usps_feed",
        content=gz,
        headers={
            "authorization": _iv_auth_header(),
            "content-type": "application/json",
            "content-encoding": "gzip",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["records"] == 1


async def test_feed_accepts_plain_when_expect_gzip_set(
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    """expect_gzip=True must NOT force decompression of an already-plain
    body. Gzip is auto-detected from the magic bytes; an operator who
    flipped the checkbox while USPS is still set to "Un-zipped" should
    NOT cause every delivery to fail. (This was the self-test bug.)"""
    await _enable_ingest(db_sessionmaker, expect_gzip=True)
    resp = await anon_client.post(
        "/usps_feed",
        json={"data": [{"imb": "no-match"}]},
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["records"] == 1


async def test_feed_decompresses_gzip_when_expect_gzip_unset(
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    """Conversely: a gzipped body still decompresses even if the operator
    never ticked the expect_gzip box and USPS forgot the
    Content-Encoding header. The magic bytes carry the truth."""
    import gzip
    import json as json_mod

    await _enable_ingest(db_sessionmaker, expect_gzip=False)
    raw = json_mod.dumps({"data": [{"imb": "no-match"}]}).encode()
    gz = gzip.compress(raw)
    resp = await anon_client.post(
        "/usps_feed",
        content=gz,
        headers={
            "authorization": _iv_auth_header(),
            "content-type": "application/json",
            # No content-encoding header on purpose.
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["records"] == 1


async def test_feed_writes_ingest_log(
    anon_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    from mailtrace.models import IngestLog

    await _enable_ingest(db_sessionmaker)
    await anon_client.post(
        "/usps_feed",
        json={"data": [{"imb": "no-match"}], "feedId": "abc-123"},
        headers={"authorization": _iv_auth_header()},
    )
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select(IngestLog))).scalars().all())
    assert len(rows) == 1
    assert rows[0].status == "parsed"
    assert rows[0].feed_id == "abc-123"
    assert rows[0].record_count == 1
    assert rows[0].matched == 0
    assert rows[0].orphaned == 1


async def test_feed_archives_raw_payload_to_disk(
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    settings: Settings,
) -> None:
    from pathlib import Path

    await _enable_ingest(db_sessionmaker, archive=True)
    await anon_client.post(
        "/usps_feed",
        json={"data": [{"imb": "no-match"}]},
        headers={"authorization": _iv_auth_header()},
    )
    archive_root = Path(settings.ingest_archive_dir)
    files = list(archive_root.rglob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes().startswith(b"{")


async def test_admin_ingest_form_renders(admin_client: AsyncClient) -> None:
    page = await admin_client.get("/admin/ingest")
    assert page.status_code == 200
    assert "USPS push feed" in page.text


async def test_admin_ingest_save_and_rotate(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    # Initial save with a chosen password.
    resp = await admin_client.post(
        "/admin/ingest",
        data={
            "enabled": "true",
            "basic_auth_user": "iv_user",
            "basic_auth_pass": "manually-chosen-very-strong-pwd-1234",
            "max_body_mb": "50",
            "archive_payloads": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    from mailtrace.models import IngestSubscription

    async with db_sessionmaker() as db:
        cfg = (
            await db.execute(select(IngestSubscription).where(IngestSubscription.id == 1))
        ).scalar_one()
        assert cfg.enabled is True
        assert cfg.basic_auth_user == "iv_user"
        assert cfg.basic_auth_pass == "manually-chosen-very-strong-pwd-1234"
        assert cfg.max_body_mb == 50
        old_pw = cfg.basic_auth_pass

    # Rotate generates a new password (not equal to the old one).
    resp = await admin_client.post(
        "/admin/ingest",
        data={
            "enabled": "true",
            "basic_auth_user": "iv_user",
            "rotate_password": "true",
            "leave_password_unchanged": "true",  # ignored when rotate set
            "max_body_mb": "50",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        cfg = (
            await db.execute(select(IngestSubscription).where(IngestSubscription.id == 1))
        ).scalar_one()
        assert cfg.basic_auth_pass != old_pw
        assert len(cfg.basic_auth_pass) >= 32


async def test_admin_ingest_save_rejects_enable_without_creds(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.post(
        "/admin/ingest",
        data={"enabled": "true", "max_body_mb": "10"},
    )
    assert resp.status_code == 400


async def test_admin_ingest_only_admin(client: AsyncClient) -> None:
    page = await client.get("/admin/ingest", follow_redirects=False)
    assert page.status_code == 403


# ---------------------------------------------------------------------------
# /admin/settings (AppConfig: USPS API creds + poll cadence)
# ---------------------------------------------------------------------------


async def test_admin_settings_form_renders(admin_client: AsyncClient) -> None:
    page = await admin_client.get("/admin/settings")
    assert page.status_code == 200
    # The page is platform-only now — credentials live per-user.
    assert "Background poller cadence" in page.text
    assert "USPS API credentials are NOT here" in page.text


async def test_admin_settings_save_persists_cadence(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await admin_client.post(
        "/admin/settings",
        data={
            "poll_enabled": "true",
            "poll_loop_interval_seconds": "600",
            "poll_max_per_cycle": "25",
            "auto_archive_after_days": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mailtrace.models import AppConfig

    async with db_sessionmaker() as db:
        cfg = (await db.execute(select(AppConfig).where(AppConfig.id == 1))).scalar_one()
        assert cfg.poll_enabled is True
        assert cfg.poll_loop_interval_seconds == 600
        assert cfg.poll_max_per_cycle == 25
        assert cfg.auto_archive_after_days == 30


async def test_admin_settings_does_not_take_credentials(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """Even if someone POSTs credential fields, the admin settings form
    silently ignores them (they live on User, not AppConfig)."""
    await admin_client.post(
        "/admin/settings",
        data={
            "usps_client_id": "should-be-ignored",
            "bcg_username": "should-also-be-ignored",
            "poll_enabled": "true",
            "poll_loop_interval_seconds": "300",
            "poll_max_per_cycle": "10",
            "auto_archive_after_days": "60",
        },
    )
    from mailtrace.models import AppConfig

    async with db_sessionmaker() as db:
        cfg = (await db.execute(select(AppConfig).where(AppConfig.id == 1))).scalar_one()
        # No usps_client_id attribute — column was removed.
        assert not hasattr(cfg, "usps_client_id")


async def test_admin_settings_rejects_invalid_intervals(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        "/admin/settings",
        data={
            "usps_client_id": "",
            "usps_client_secret": "",
            "bcg_username": "",
            "bcg_password": "",
            "poll_enabled": "false",
            "poll_loop_interval_seconds": "5",  # below min 30
            "poll_max_per_cycle": "10",
            "auto_archive_after_days": "60",
        },
    )
    assert resp.status_code == 400


async def test_admin_settings_only_admin(client: AsyncClient) -> None:
    page = await client.get("/admin/settings", follow_redirects=False)
    assert page.status_code == 403


# ---------------------------------------------------------------------------
# Per-user setup wizard at /auth/account (USPS API creds + BCG + tests)
# ---------------------------------------------------------------------------


async def test_account_form_renders_setup_sections(client: AsyncClient) -> None:
    page = await client.get("/auth/account")
    assert page.status_code == 200
    assert "USPS API credentials" in page.text
    assert "BCG credentials" in page.text
    assert "Mailer ID" in page.text


async def test_account_save_persists_per_user_creds(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    resp = await client.post(
        "/auth/account",
        data={
            "mailer_id": "555000",
            "barcode_id": "0",
            "service_type_id": "40",
            "usps_client_id": "consumer-key",
            "usps_client_secret": "consumer-secret",
            "bcg_username": "bcg-login",
            "bcg_password": "bcg-pass",
            "notify_on_scans": "true",
            "notify_email": "alerts@example.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        u = await db.get(User, regular_user.id)
        assert u is not None
        assert u.mailer_id == 555000
        assert u.usps_client_id == "consumer-key"
        assert u.usps_client_secret == "consumer-secret"
        assert u.bcg_username == "bcg-login"
        assert u.bcg_password == "bcg-pass"
        assert u.notify_on_scans is True
        assert u.notify_email == "alerts@example.com"


async def test_account_leave_secret_unchanged(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    # First save: real values.
    await client.post(
        "/auth/account",
        data={
            "mailer_id": "1",
            "barcode_id": "0",
            "service_type_id": "40",
            "usps_client_id": "k",
            "usps_client_secret": "REAL-SECRET",
            "bcg_username": "u",
            "bcg_password": "REAL-PASS",
        },
    )
    # Second save: leave-unchanged → originals retained.
    await client.post(
        "/auth/account",
        data={
            "mailer_id": "1",
            "barcode_id": "0",
            "service_type_id": "40",
            "usps_client_id": "k2",
            "usps_client_secret": "",
            "bcg_username": "u2",
            "bcg_password": "",
            "leave_usps_secret_unchanged": "true",
            "leave_bcg_password_unchanged": "true",
        },
    )
    async with db_sessionmaker() as db:
        u = await db.get(User, regular_user.id)
        assert u is not None
        assert u.usps_client_id == "k2"
        assert u.usps_client_secret == "REAL-SECRET"
        assert u.bcg_username == "u2"
        assert u.bcg_password == "REAL-PASS"


async def test_account_test_usps_api_button_writes_probe_result(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    fake_usps: Any,
) -> None:
    """The Test USPS API button calls probe_modern_creds and stores the
    one-line result on the user."""
    fake_usps.tracking_error = None  # success path
    resp = await client.post("/auth/account/test-usps-api", follow_redirects=False)
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        u = await db.get(User, regular_user.id)
        assert u is not None
        assert u.usps_api_last_check == "ok"


async def test_account_test_usps_api_records_failure(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    fake_usps: Any,
) -> None:
    from mailtrace.usps import USPSError

    fake_usps.tracking_error = USPSError("invalid client_id")
    resp = await client.post("/auth/account/test-usps-api", follow_redirects=False)
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        u = await db.get(User, regular_user.id)
        assert u is not None
        assert u.usps_api_last_check.startswith("fail:")
        assert "invalid client_id" in u.usps_api_last_check


async def test_account_test_bcg_button(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    fake_usps: Any,
) -> None:
    fake_usps.tracking_error = None
    resp = await client.post("/auth/account/test-bcg", follow_redirects=False)
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        u = await db.get(User, regular_user.id)
        assert u is not None
        assert u.bcg_last_check == "ok"


async def test_account_test_notify_without_smtp_config(
    client: AsyncClient,
) -> None:
    """If admin hasn't set up SMTP, the user-side test reports it gracefully."""
    resp = await client.post("/auth/account/test-notify", follow_redirects=False)
    assert resp.status_code == 303
    page = await client.get("/auth/account")
    # The apostrophe gets HTML-escaped by Jinja, so look for a stable substring.
    assert "configured SMTP" in page.text


async def test_validate_address_uses_current_user_creds(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    fake_usps: Any,
) -> None:
    """The validate endpoint passes the logged-in user to the USPS client.
    With our FakeUSPS that just echoes back, no creds are required for
    the stub to succeed — but the User object is what gets passed in."""
    captured: dict[str, Any] = {}
    real_standardize = fake_usps.standardize_address

    async def capturing(user: Any, address: dict[str, Any]) -> Any:
        captured["user_id"] = user.id
        return await real_standardize(user, address)

    fake_usps.standardize_address = capturing  # type: ignore[method-assign]
    resp = await client.post(
        "/addresses/validate",
        data={"street_address": "1 Main", "city": "X", "state": "CA", "zip": "94105"},
    )
    assert resp.status_code == 200
    assert captured["user_id"] == regular_user.id


async def test_admin_ingest_self_test_loops_through_local_endpoint(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
) -> None:
    await _enable_ingest(db_sessionmaker)
    resp = await admin_client.post("/admin/ingest/test", follow_redirects=False)
    assert resp.status_code == 303
    # Self-test produced an IngestLog entry.
    from mailtrace.models import IngestLog

    async with db_sessionmaker() as db:
        rows = list((await db.execute(select(IngestLog))).scalars().all())
    assert any(r.feed_id == "self-test" for r in rows)


# ---------------------------------------------------------------------------
# First-run setup
# ---------------------------------------------------------------------------


async def test_setup_redirects_when_db_empty(empty_db_client: AsyncClient) -> None:
    resp = await empty_db_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/setup")


async def test_setup_form_renders_when_db_empty(empty_db_client: AsyncClient) -> None:
    resp = await empty_db_client.get("/setup")
    assert resp.status_code == 200
    assert "first administrator" in resp.text


async def test_setup_creates_first_admin(
    empty_db_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await empty_db_client.post(
        "/setup",
        data={
            "email": "boss@example.com",
            "password": "first-admin-password",
            "confirm_password": "first-admin-password",
            "mailer_id": "555",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/auth/login")

    from sqlalchemy import select

    from mailtrace.models import User as UserModel

    async with db_sessionmaker() as db:
        u = (
            await db.execute(select(UserModel).where(UserModel.email == "boss@example.com"))
        ).scalar_one()
        assert u.is_admin is True
        assert u.must_change_password is False  # they typed it themselves
        assert u.mailer_id == 555

    # Setup form is now gone.
    again = await empty_db_client.get("/setup", follow_redirects=False)
    assert again.status_code == 404


async def test_setup_rejects_after_users_exist(anon_client: AsyncClient) -> None:
    # Fixture (regular_user) has already created a user.
    resp = await anon_client.get("/setup", follow_redirects=False)
    assert resp.status_code == 404
    resp2 = await anon_client.post(
        "/setup",
        data={
            "email": "race@example.com",
            "password": "race-password-1234",
            "confirm_password": "race-password-1234",
        },
        follow_redirects=False,
    )
    assert resp2.status_code == 409


async def test_setup_validates_password_match(empty_db_client: AsyncClient) -> None:
    resp = await empty_db_client.post(
        "/setup",
        data={
            "email": "x@y.com",
            "password": "hunter2hunter2",
            "confirm_password": "different-pwd",
        },
    )
    assert resp.status_code == 400
    assert "do not match" in resp.text


# ---------------------------------------------------------------------------
# Admin extensions: edit, delete, custom-password reset
# ---------------------------------------------------------------------------


async def test_admin_edit_user_updates_mid_and_email(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    resp = await admin_client.post(
        f"/admin/users/{regular_user.id}/edit",
        data={
            "email": "renamed@example.com",
            "mailer_id": "987654",
            "barcode_id": "1",
            "service_type_id": "42",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mailtrace.models import User as UserModel

    async with db_sessionmaker() as db:
        u = await db.get(UserModel, regular_user.id)
        assert u is not None
        assert u.email == "renamed@example.com"
        assert u.mailer_id == 987654
        assert u.barcode_id == 1
        assert u.service_type_id == 42


async def test_admin_edit_rejects_email_collision(
    admin_client: AsyncClient, regular_user: User, admin_user: User
) -> None:
    resp = await admin_client.post(
        f"/admin/users/{regular_user.id}/edit",
        data={
            "email": admin_user.email,
            "mailer_id": "1",
            "barcode_id": "0",
            "service_type_id": "40",
        },
    )
    assert resp.status_code == 400
    assert "Another user already has that email" in resp.text


async def test_admin_force_reset_with_custom_password(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    chosen = "operator-chose-this"
    resp = await admin_client.post(
        f"/admin/users/{regular_user.id}/force-reset",
        data={"custom_password": chosen},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mailtrace import auth as auth_lib
    from mailtrace.models import User as UserModel

    async with db_sessionmaker() as db:
        u = await db.get(UserModel, regular_user.id)
        assert u is not None
        assert u.must_change_password is True
        assert auth_lib.verify_password(chosen, u.password_hash)


async def test_admin_force_reset_custom_password_too_short(
    admin_client: AsyncClient, regular_user: User
) -> None:
    resp = await admin_client.post(
        f"/admin/users/{regular_user.id}/force-reset",
        data={"custom_password": "short"},
    )
    assert resp.status_code == 400


async def test_admin_delete_user(
    admin_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    resp = await admin_client.post(f"/admin/users/{regular_user.id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    from mailtrace.models import User as UserModel

    async with db_sessionmaker() as db:
        u = await db.get(UserModel, regular_user.id)
        assert u is None


async def test_admin_cannot_delete_self(admin_client: AsyncClient, admin_user: User) -> None:
    resp = await admin_client.post(f"/admin/users/{admin_user.id}/delete", follow_redirects=False)
    assert resp.status_code == 400


async def test_admin_cannot_delete_last_admin(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    admin_user: User,
    regular_user: User,
) -> None:
    """Two admins → demote one → second is now the only active admin and
    cannot be deleted by anyone (here, the last admin themselves)."""
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, admin_user.email, "admin-password-12345")
        # Promote regular_user, log in as them, demote admin_user, then try
        # to delete admin_user (who is no longer an admin).
        await ac.post(f"/admin/users/{regular_user.id}/toggle-admin", follow_redirects=False)
        # Now demote admin_user via the second user's session.
        await ac.post("/auth/logout", follow_redirects=False)
        await _login(ac, regular_user.email, "user-password-12345")
        await ac.post(f"/admin/users/{admin_user.id}/toggle-admin", follow_redirects=False)
        # regular_user is now the only admin. Try to delete them via the
        # demoted-admin's session (which can no longer reach /admin/).
        forbidden = await ac.post(f"/admin/users/{regular_user.id}/delete", follow_redirects=False)
        # We're logged in as regular_user; deleting self → 400.
        assert forbidden.status_code == 400
    await redis.aclose()


# ---------------------------------------------------------------------------
# Sticker sheet
# ---------------------------------------------------------------------------


async def test_sticker_sheet_renders_html(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    # Create three pieces.
    pids = []
    for label in ("a", "b", "c"):
        resp = await client.post(
            "/pieces/new",
            data={
                "label": label,
                "recipient_name": "Bob",
                "recipient_street": "200 Market St",
                "recipient_city": "Sometown",
                "recipient_state": "CA",
                "recipient_zip": "94105",
                "include_zip_in_imb": "true",
            },
            follow_redirects=False,
        )
        pids.append(int(resp.headers["location"].rsplit("/", 1)[-1]))

    # Render starting at row 2, col 2 — page 1 has 2 cells used (r2c2, r3c1),
    # third cell r3c2.
    sheet = await client.post(
        "/pieces/sheet",
        data={
            "ids": [str(p) for p in pids],
            "start_row": "2",
            "start_col": "2",
            "doc_type": "html",
        },
    )
    assert sheet.status_code == 200
    assert "USPSIMBStandard" in sheet.text
    # First piece is at row 2, col 2 → top = 0.5 + 1*2 = 2.5in, left = 0.2 + 1*4.25 = 4.45in
    assert "top: 2.500in" in sheet.text
    assert "left: 4.450in" in sheet.text


async def test_sticker_sheet_paginates_when_overflow(
    client: AsyncClient,
) -> None:
    # 12 pieces, sheet holds 10 → should produce 2 pages.
    pids = []
    for i in range(12):
        resp = await client.post(
            "/pieces/new",
            data={
                "label": f"p{i}",
                "recipient_name": "Bob",
                "recipient_street": "200 Market St",
                "recipient_city": "Sometown",
                "recipient_state": "CA",
                "recipient_zip": "94105",
                "include_zip_in_imb": "true",
            },
            follow_redirects=False,
        )
        pids.append(int(resp.headers["location"].rsplit("/", 1)[-1]))
    sheet = await client.post(
        "/pieces/sheet",
        data={
            "ids": [str(p) for p in pids],
            "start_row": "1",
            "start_col": "1",
            "doc_type": "html",
        },
    )
    assert sheet.status_code == 200
    # Two <div class="page"> blocks.
    assert sheet.text.count('class="page"') == 2


async def test_sticker_sheet_rejects_other_users_pieces(
    settings: Settings,
    fake_usps: Any,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    admin_user: User,
) -> None:
    redis = FakeAsyncRedis()
    store = Store(redis, rolling_window_days=50, event_ttl_seconds=60)
    app = _build_app(settings, store, fake_usps, db_sessionmaker)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, regular_user.email, "user-password-12345")
        create = await ac.post(
            "/pieces/new",
            data={
                "recipient_name": "Bob",
                "recipient_street": "200 Market St",
                "recipient_city": "Sometown",
                "recipient_state": "CA",
                "recipient_zip": "94105",
                "include_zip_in_imb": "true",
            },
            follow_redirects=False,
        )
        pid = int(create.headers["location"].rsplit("/", 1)[-1])

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _login(ac, admin_user.email, "admin-password-12345")
        sheet = await ac.post(
            "/pieces/sheet",
            data={
                "ids": [str(pid)],
                "start_row": "1",
                "start_col": "1",
                "doc_type": "html",
            },
        )
        assert sheet.status_code == 404
    await redis.aclose()


# ---------------------------------------------------------------------------
# SMTP config admin page
# ---------------------------------------------------------------------------


async def test_admin_email_form_shows_provider_guides(
    admin_client: AsyncClient,
) -> None:
    page = await admin_client.get("/admin/email")
    assert page.status_code == 200
    assert "smtp.office365.com" in page.text
    assert "smtp.gmail.com" in page.text
    assert "STARTTLS" in page.text


async def test_admin_email_save_persists(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await admin_client.post(
        "/admin/email",
        data={
            "host": "smtp.example.com",
            "port": "587",
            "username": "u",
            "password": "p",
            "encryption": "starttls",
            "from_address": "noreply@example.com",
            "from_name": "mailtrace",
            "public_base_url": "https://mt.example.com",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from mailtrace.models import SmtpConfig

    async with db_sessionmaker() as db:
        cfg = (await db.execute(select(SmtpConfig))).scalar_one()
        assert cfg.host == "smtp.example.com"
        assert cfg.password == "p"
        assert cfg.enabled is True


async def test_admin_email_keeps_password_when_box_unchanged(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    # Save once.
    await admin_client.post(
        "/admin/email",
        data={
            "host": "smtp.example.com",
            "port": "587",
            "username": "u",
            "password": "secret-original",
            "encryption": "starttls",
            "from_address": "noreply@example.com",
            "enabled": "true",
        },
        follow_redirects=False,
    )
    # Save again with leave_password_unchanged.
    await admin_client.post(
        "/admin/email",
        data={
            "host": "smtp.example.com",
            "port": "587",
            "username": "u",
            "password": "",
            "encryption": "starttls",
            "from_address": "noreply@example.com",
            "leave_password_unchanged": "true",
            "enabled": "true",
        },
        follow_redirects=False,
    )

    from mailtrace.models import SmtpConfig

    async with db_sessionmaker() as db:
        cfg = (await db.execute(select(SmtpConfig))).scalar_one()
        assert cfg.password == "secret-original"


async def test_admin_email_only_admin(client: AsyncClient) -> None:
    page = await client.get("/admin/email", follow_redirects=False)
    assert page.status_code == 403


# ---------------------------------------------------------------------------
# Phase 4: address validation, CSV import, healthz DB check
# ---------------------------------------------------------------------------


async def test_healthz_passes_when_redis_and_db_ok(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_validate_address_endpoint_returns_standardized(
    client: AsyncClient, fake_usps: Any
) -> None:
    """The /addresses/validate endpoint defers to the USPS client. The
    fake_usps fixture echoes the input back with zip4=0001, dp=00."""
    resp = await client.post(
        "/addresses/validate",
        data={
            "firmname": "",
            "address2": "",
            "street_address": "200 Market St",
            "city": "San Francisco",
            "state": "CA",
            "zip": "94105",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("error") is None
    assert body["address"]["zip4"] == "0001"
    assert body["address"]["dp"] == "00"


async def test_validate_address_unauthenticated_redirects_to_login(
    anon_client: AsyncClient,
) -> None:
    resp = await anon_client.post(
        "/addresses/validate",
        data={"street_address": "x", "city": "x", "state": "CA", "zip": "94105"},
        follow_redirects=False,
    )
    # Auth middleware kicks in before the route — POST without text/html accept
    # gets a 401 (not redirected); browsers would get redirected.
    assert resp.status_code in (302, 401)


async def test_csv_import_creates_pieces_and_addresses(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    csv_text = (
        "label,name,street,city,state,zip,include_zip_in_imb\n"
        "card 1,Alice,123 Main St,Springfield,IL,62701,true\n"
        "card 2,Bob,456 Oak Ave,Anytown,CA,94105,true\n"
        "card 3,Carol,789 Pine Rd,Pasadena,CA,91101,false\n"
    )
    resp = await client.post(
        "/pieces/import",
        data={
            "csv_text": csv_text,
            "save_addresses": "true",
            "include_zip_in_imb": "true",
        },
    )
    assert resp.status_code == 200
    assert "Imported 3 piece(s)" in resp.text
    assert "Saved 3 new address" in resp.text

    from mailtrace.models import Address as AddressModel

    async with db_sessionmaker() as db:
        pieces = list(
            (await db.execute(select(MailPiece).where(MailPiece.user_id == regular_user.id)))
            .scalars()
            .all()
        )
        assert len(pieces) == 3
        labels = sorted(p.label for p in pieces)
        assert labels == ["card 1", "card 2", "card 3"]
        # Per-row include_zip_in_imb: row 3 set to false should drop the zip.
        carol = next(p for p in pieces if p.label == "card 3")
        assert carol.include_zip_in_imb is False
        assert not carol.imb_raw.endswith("91101")
        # First two should include the zip.
        alice = next(p for p in pieces if p.label == "card 1")
        assert alice.imb_raw.endswith("62701")

        addrs = list(
            (await db.execute(select(AddressModel).where(AddressModel.user_id == regular_user.id)))
            .scalars()
            .all()
        )
        assert len(addrs) == 3


async def test_csv_import_partial_errors_still_commit_good_rows(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    csv_text = (
        "label,name,street,city,state,zip\n"
        "good,Alice,123 Main St,Springfield,IL,62701\n"
        "bad-zip,Bob,456 Oak Ave,Anytown,CA,12\n"
    )
    resp = await client.post(
        "/pieces/import",
        data={"csv_text": csv_text, "save_addresses": "false"},
    )
    assert resp.status_code == 200
    assert "Imported 1 piece(s)" in resp.text
    assert "had problems" in resp.text


async def test_csv_import_missing_required_column_rejects_whole_file(
    client: AsyncClient,
) -> None:
    csv_text = "label,name\nx,Alice\n"
    resp = await client.post(
        "/pieces/import",
        data={"csv_text": csv_text},
    )
    assert resp.status_code == 400
    assert "missing required columns" in resp.text


async def test_csv_import_label_collision_gets_unique_suffix(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    # Pre-create an address book entry with the label "shared".
    from mailtrace.models import Address as AddressModel

    async with db_sessionmaker() as db:
        db.add(AddressModel(user_id=regular_user.id, label="shared", role="recipient", zip="00000"))
        await db.commit()

    csv_text = (
        "label,name,street,city,state,zip\n"
        "shared,Bob,1 St,X,CA,94105\n"
        "shared,Carol,2 St,X,CA,94106\n"
    )
    resp = await client.post(
        "/pieces/import",
        data={"csv_text": csv_text, "save_addresses": "true"},
    )
    assert resp.status_code == 200

    async with db_sessionmaker() as db:
        labels = sorted(
            (
                await db.execute(
                    select(AddressModel.label).where(AddressModel.user_id == regular_user.id)
                )
            )
            .scalars()
            .all()
        )
    # Original "shared" + auto-disambiguated copies.
    assert "shared" in labels
    assert "shared (2)" in labels
    assert "shared (3)" in labels


async def test_csv_import_empty_text_renders_error(client: AsyncClient) -> None:
    resp = await client.post("/pieces/import", data={"csv_text": ""})
    assert resp.status_code == 400
    assert "Paste at least one row" in resp.text


# ---------------------------------------------------------------------------
# Stock lifecycle: generated → printed → in_flight → delivered
# ---------------------------------------------------------------------------


async def test_single_create_defaults_to_in_flight(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    """Default behavior of /pieces/new is unchanged: immediate-mail."""
    resp = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
        },
        follow_redirects=False,
    )
    pid = int(resp.headers["location"].rsplit("/", 1)[-1])
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "in_flight"
        assert piece.mailed_at is not None
        assert piece.next_poll_at is not None


async def test_single_create_keep_as_stock(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    resp = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
            "keep_as_stock": "true",
        },
        follow_redirects=False,
    )
    pid = int(resp.headers["location"].rsplit("/", 1)[-1])
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "generated"
        assert piece.mailed_at is None
        assert piece.printed_at is None
        assert piece.next_poll_at is None  # stock isn't polled


async def test_batch_with_count_generates_n_per_recipient(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id

    resp = await client.post(
        "/pieces/batch",
        data={
            "row-0-label": "weekly card",
            "row-0-recipient_id": str(a_id),
            "row-0-count": "5",
            "row-0-include_zip": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        assert len(rows) == 5
        # All in stock state, all share the label.
        assert all(p.status == "generated" for p in rows)
        assert all(p.label == "weekly card" for p in rows)
        # Each gets a unique serial.
        assert len({p.serial for p in rows}) == 5


async def test_batch_count_clamped_to_limit(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id

    resp = await client.post(
        "/pieces/batch",
        data={
            "row-0-recipient_id": str(a_id),
            "row-0-count": "9999",
        },
    )
    # Only one row, and it errored → 400 page re-render.
    assert resp.status_code == 400
    assert "exceeds per-row limit" in resp.text


async def test_batch_mark_as_mailed_form_flag(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id

    resp = await client.post(
        "/pieces/batch",
        data={
            "row-0-recipient_id": str(a_id),
            "row-0-count": "2",
            "mark_as_mailed": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        assert len(rows) == 2
        assert all(p.status == "in_flight" for p in rows)
        assert all(p.mailed_at is not None for p in rows)


async def test_mark_printed_then_mailed(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    create = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
            "keep_as_stock": "true",
        },
        follow_redirects=False,
    )
    pid = int(create.headers["location"].rsplit("/", 1)[-1])

    print_resp = await client.post(f"/pieces/{pid}/mark-printed", follow_redirects=False)
    assert print_resp.status_code == 303
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "printed"
        assert piece.printed_at is not None
        assert piece.mailed_at is None

    mail_resp = await client.post(f"/pieces/{pid}/mark-mailed", follow_redirects=False)
    assert mail_resp.status_code == 303
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "in_flight"
        assert piece.mailed_at is not None
        assert piece.next_poll_at is not None


async def test_bulk_mark_mailed(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id
    # Create 3 stock pieces via batch.
    await client.post(
        "/pieces/batch",
        data={"row-0-recipient_id": str(a_id), "row-0-count": "3"},
        follow_redirects=False,
    )
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        ids = [str(p.id) for p in rows]

    resp = await client.post(
        "/pieces/bulk-action",
        data={"ids": ids, "action": "mark_mailed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        assert all(p.status == "in_flight" for p in rows)


async def test_status_filter_tabs(
    client: AsyncClient, db_sessionmaker: async_sessionmaker, regular_user: User
) -> None:
    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id
    # 2 stock + 1 in_flight (via /pieces/new default).
    await client.post(
        "/pieces/batch",
        data={"row-0-recipient_id": str(a_id), "row-0-count": "2"},
        follow_redirects=False,
    )
    await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
        },
        follow_redirects=False,
    )

    listing = await client.get("/pieces/?status=generated")
    assert listing.status_code == 200
    # Counts in tab labels.
    assert "Stock (2)" in listing.text
    assert "In flight (1)" in listing.text

    only_in_flight = await client.get("/pieces/?status=in_flight")
    # The 2 stock pieces' rows should not appear in this filter.
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        stock_ids = [p.id for p in rows if p.status == "generated"]
    for sid in stock_ids:
        assert f"/pieces/{sid}" not in only_in_flight.text


async def test_sheet_pdf_marks_generated_as_printed(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering a sticker sheet as HTML doesn't change state, but rendering
    as PDF transitions stock pieces to printed."""
    # Stub out wkhtmltopdf — we only care about the state side-effect.
    from mailtrace import pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "render", lambda html, *, options: b"%PDF-stub")

    from mailtrace.models import Address

    async with db_sessionmaker() as db:
        a = Address(user_id=regular_user.id, label="alice", role="recipient", zip="94105")
        db.add(a)
        await db.commit()
        await db.refresh(a)
        a_id = a.id
    await client.post(
        "/pieces/batch",
        data={"row-0-recipient_id": str(a_id), "row-0-count": "2"},
        follow_redirects=False,
    )
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        ids = [str(p.id) for p in rows]

    # HTML preview: no state change.
    html_resp = await client.post(
        "/pieces/sheet",
        data={"ids": ids, "start_row": "1", "start_col": "1", "doc_type": "html"},
    )
    assert html_resp.status_code == 200
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        assert all(p.status == "generated" for p in rows)

    # PDF render: transitions generated → printed.
    pdf_resp = await client.post(
        "/pieces/sheet",
        data={"ids": ids, "start_row": "1", "start_col": "1", "doc_type": "pdf"},
    )
    assert pdf_resp.status_code == 200
    async with db_sessionmaker() as db:
        rows = list((await db.execute(select_all_user_pieces(regular_user.id))).scalars().all())
        assert all(p.status == "printed" for p in rows)
        assert all(p.printed_at is not None for p in rows)


async def test_scan_promotes_stock_piece_to_in_flight(
    client: AsyncClient,
    anon_client: AsyncClient,
    db_sessionmaker: async_sessionmaker,
    regular_user: User,
) -> None:
    """A scan landing on a generated/printed piece via the feed should
    auto-promote it to in_flight (USPS already has it)."""
    await _enable_ingest(db_sessionmaker)
    create = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
            "keep_as_stock": "true",
        },
        follow_redirects=False,
    )
    pid = int(create.headers["location"].rsplit("/", 1)[-1])
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "generated"
        imb = piece.imb_raw

    resp = await anon_client.post(
        "/usps_feed",
        json={
            "data": [
                {
                    "imb": imb,
                    "scanDateTime": "2025-02-15T10:00:00Z",
                    "scanEventCode": "SP",
                    "scanFacilityCity": "San Francisco",
                    "scanFacilityState": "CA",
                }
            ]
        },
        headers={"authorization": _iv_auth_header()},
    )
    assert resp.status_code == 200
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "in_flight"
        assert piece.mailed_at is not None
        assert piece.next_poll_at is not None


async def test_archive_unarchive_restores_pre_archive_status(
    client: AsyncClient, db_sessionmaker: async_sessionmaker
) -> None:
    """A printed piece that's archived should come back as printed."""
    create = await client.post(
        "/pieces/new",
        data={
            "recipient_name": "Bob",
            "recipient_street": "200 Market St",
            "recipient_city": "Sometown",
            "recipient_state": "CA",
            "recipient_zip": "94105",
            "include_zip_in_imb": "true",
            "keep_as_stock": "true",
        },
        follow_redirects=False,
    )
    pid = int(create.headers["location"].rsplit("/", 1)[-1])
    await client.post(f"/pieces/{pid}/mark-printed", follow_redirects=False)
    await client.post(f"/pieces/{pid}/archive", follow_redirects=False)
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "archived"
        assert piece.pre_archive_status == "printed"

    await client.post(f"/pieces/{pid}/unarchive", follow_redirects=False)
    async with db_sessionmaker() as db:
        piece = await db.get(MailPiece, pid)
        assert piece is not None
        assert piece.status == "printed"
        assert piece.pre_archive_status == ""
        assert piece.next_poll_at is None  # printed isn't polled
