"""Unit tests for the domain services layer (no HTTP)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import async_sessionmaker

from mailtrace import services
from mailtrace.models import (
    STATUS_DELIVERED,
    STATUS_IN_FLIGHT,
    Address,
    MailPiece,
    User,
    utcnow,
)
from mailtrace.services import PieceDraft, PieceValidationError
from mailtrace.store import Store

# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def _piece(**overrides):  # type: ignore[no-untyped-def]
    base = MailPiece(
        user_id=1,
        sender_block="",
        recipient_block="x",
        recipient_zip_raw="94105",
        barcode_id=0,
        service_type_id=40,
        mailer_id=314159,
        serial=1,
        include_zip_in_imb=True,
        imb_letters="A" * 65,
        imb_raw="0004031415900000194105",
        status=STATUS_IN_FLIGHT,
        consecutive_poll_errors=0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_cadence_brand_new_piece_is_15_min() -> None:
    now = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.UTC)
    p = _piece(created_at=now - dt.timedelta(minutes=10))
    target = services.next_poll_at_for(p, now=now)
    assert target == now + dt.timedelta(minutes=15)


def test_cadence_two_day_old_piece_is_2h() -> None:
    now = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.UTC)
    p = _piece(created_at=now - dt.timedelta(days=3))
    target = services.next_poll_at_for(p, now=now)
    assert target == now + dt.timedelta(hours=2)


def test_cadence_stale_piece_is_6h() -> None:
    now = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.UTC)
    p = _piece(created_at=now - dt.timedelta(days=10))
    target = services.next_poll_at_for(p, now=now)
    assert target == now + dt.timedelta(hours=6)


def test_cadence_terminal_piece_returns_none() -> None:
    p = _piece(status=STATUS_DELIVERED)
    assert services.next_poll_at_for(p) is None


def test_cadence_backoff_grows_with_errors() -> None:
    now = dt.datetime(2025, 1, 1, 12, 0, tzinfo=dt.UTC)
    p1 = _piece(created_at=now - dt.timedelta(minutes=5), consecutive_poll_errors=1)
    p2 = _piece(created_at=now - dt.timedelta(minutes=5), consecutive_poll_errors=4)
    t1 = services.next_poll_at_for(p1, now=now)
    t2 = services.next_poll_at_for(p2, now=now)
    assert t1 is not None and t2 is not None
    assert (t2 - now) > (t1 - now)
    # cap at 6h
    p_high = _piece(created_at=now, consecutive_poll_errors=20)
    t_high = services.next_poll_at_for(p_high, now=now)
    assert t_high is not None
    assert t_high - now == dt.timedelta(hours=6)


# ---------------------------------------------------------------------------
# Piece creation + scan ingestion
# ---------------------------------------------------------------------------


async def test_create_piece_requires_mailer_id(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(
            email="x@example.com",
            password_hash="x",
            mailer_id=None,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)

        try:
            await services.create_piece(
                db,
                store=store,
                user=u,
                draft=PieceDraft(
                    recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                    recipient_zip_inline="94105",
                ),
            )
        except PieceValidationError as err:
            assert "Mailer ID" in str(err)
        else:
            raise AssertionError("expected PieceValidationError")


async def test_create_piece_zipless_imb_drops_zip_suffix(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(email="y@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)

        piece_with = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
                include_zip_in_imb=True,
            ),
        )
        piece_without = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
                include_zip_in_imb=False,
            ),
        )
        await db.commit()
        assert piece_with.imb_raw.endswith("94105")
        assert not piece_without.imb_raw.endswith("94105")
        assert len(piece_without.imb_raw) < len(piece_with.imb_raw)


async def test_ingest_scan_dedup(db_sessionmaker: async_sessionmaker, store: Store) -> None:
    async with db_sessionmaker() as db:
        u = User(email="z@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
            ),
        )
        await db.commit()

        event = {
            "scanDatetime": "2025-01-15T10:00:00",
            "scanEventCode": "SL",
            "machineName": "AFCS",
            "scanFacilityZip": "94107",
        }
        first = await services.ingest_scan(db, piece, event, source="poll")
        second = await services.ingest_scan(db, piece, event, source="poll")
        await db.commit()
        assert first is True
        assert second is False  # dedup


async def test_ingest_scan_promotes_status_on_delivery(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(email="d@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        await db.commit()
        assert piece.status == STATUS_IN_FLIGHT
        await services.ingest_scan(
            db,
            piece,
            {"scanDatetime": "2025-01-20T15:00:00", "scanEventCode": "01"},
            source="feed",
        )
        await db.commit()
        assert piece.status == STATUS_DELIVERED


# ---------------------------------------------------------------------------
# poll_one
# ---------------------------------------------------------------------------


class _StubUSPS:
    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}
        self.error: Exception | None = None

    async def get_piece_tracking(self, user, imb: str) -> dict:  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        return self.payloads.get(imb, {"data": {"imb": imb, "scans": []}})


async def test_poll_one_records_scans_and_resets_errors(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(email="p@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        piece.consecutive_poll_errors = 3  # pretend previous errors
        await db.commit()

        usps = _StubUSPS()
        usps.payloads[piece.imb_raw] = {
            "data": {
                "imb": piece.imb_raw,
                "scans": [
                    {"scanDatetime": "2025-01-15T10:00", "scanEventCode": "SL"},
                    {"scanDatetime": "2025-01-16T11:00", "scanEventCode": "SF"},
                ],
            }
        }
        inserted, err = await services.poll_one(db, piece=piece, usps=usps)  # type: ignore[arg-type]
        await db.commit()
        assert err is None
        assert inserted == 2
        assert piece.consecutive_poll_errors == 0
        assert piece.last_polled_at is not None
        assert piece.next_poll_at is not None


async def test_poll_one_increments_errors_on_failure(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    from mailtrace.usps import USPSError

    async with db_sessionmaker() as db:
        u = User(email="e@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        await db.commit()

        usps = _StubUSPS()
        usps.error = USPSError("upstream down")
        inserted, err = await services.poll_one(db, piece=piece, usps=usps)  # type: ignore[arg-type]
        await db.commit()
        assert inserted == 0
        assert err == "upstream down"
        assert piece.consecutive_poll_errors == 1
        # Backoff put next_poll_at in the future.
        assert piece.next_poll_at is not None
        assert piece.next_poll_at > utcnow() - dt.timedelta(seconds=5)


# ---------------------------------------------------------------------------
# Due selection
# ---------------------------------------------------------------------------


async def test_select_due_pieces_skips_archived_and_terminal(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(email="due@example.com", password_hash="x", mailer_id=314159)
        db.add(u)
        await db.commit()
        await db.refresh(u)

        live = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        delivered = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        delivered.status = STATUS_DELIVERED
        archived = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
            initial_status=STATUS_IN_FLIGHT,
        )
        archived.archived_at = utcnow()
        # Stock pieces should also not appear in select_due_pieces.
        stock = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
        )
        await db.commit()

        due = await services.select_due_pieces(db, limit=10)
        ids = {p.id for p in due}
        assert live.id in ids
        assert delivered.id not in ids
        assert archived.id not in ids
        assert stock.id not in ids  # generated pieces aren't polled


async def test_address_to_block_renders_zip4() -> None:
    a = Address(
        user_id=1,
        label="x",
        role="recipient",
        name="Bob",
        street="200 Market St",
        city="Sometown",
        state="CA",
        zip="941051234",
    )
    block = a.to_recipient_block()
    assert "94105-1234" in block


# ---------------------------------------------------------------------------
# Email notification dispatcher
# ---------------------------------------------------------------------------


class _RecordingMailer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list = []
        self.fail = fail

    async def send(self, msg) -> None:  # type: ignore[no-untyped-def]
        if self.fail:
            from mailtrace.mail import MailerError

            raise MailerError("fake transport down")
        self.sent.append(msg)


def _smtp() -> object:
    from mailtrace.models import SmtpConfig

    return SmtpConfig(
        id=1,
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
        encryption="starttls",
        from_address="noreply@example.com",
        from_name="mailtrace",
        public_base_url="https://mt.example.com",
        enabled=True,
    )


async def test_dispatch_notifications_sends_digest_then_dedups(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(
            email="watcher@example.com",
            password_hash="x",
            mailer_id=314159,
            notify_on_scans=True,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                label="rent check",
                recipient_block_inline="Bob\n200 Market St\nSometown, CA, 94105",
                recipient_zip_inline="94105",
            ),
        )
        await services.ingest_scan(
            db,
            piece,
            {"scanDatetime": "2025-01-15T10:00", "scanEventCode": "SL", "scanFacilityCity": "SF"},
            source="poll",
        )
        await db.commit()

        mailer = _RecordingMailer()
        sent = await services.dispatch_notifications(db, smtp=_smtp(), mailer=mailer)
        await db.commit()
        assert sent == 1
        assert len(mailer.sent) == 1
        msg = mailer.sent[0]
        assert msg.to == "watcher@example.com"
        assert "rent check" in msg.body_text
        # last_notified_at is now bumped → second call sends nothing.
        sent_again = await services.dispatch_notifications(db, smtp=_smtp(), mailer=mailer)
        assert sent_again == 0
        assert len(mailer.sent) == 1


async def test_dispatch_notifications_skips_users_who_opted_out(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(
            email="silent@example.com",
            password_hash="x",
            mailer_id=314159,
            notify_on_scans=False,  # opted out
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
        )
        await services.ingest_scan(
            db,
            piece,
            {"scanDatetime": "2025-01-15T10:00", "scanEventCode": "SL"},
            source="poll",
        )
        await db.commit()

        mailer = _RecordingMailer()
        sent = await services.dispatch_notifications(db, smtp=_smtp(), mailer=mailer)
        assert sent == 0
        assert mailer.sent == []


async def test_dispatch_notifications_does_not_bump_on_send_failure(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(
            email="retry@example.com",
            password_hash="x",
            mailer_id=314159,
            notify_on_scans=True,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
        )
        await services.ingest_scan(
            db,
            piece,
            {"scanDatetime": "2025-01-15T10:00", "scanEventCode": "SL"},
            source="poll",
        )
        await db.commit()

        # First dispatch fails → last_notified_at stays None.
        bad = _RecordingMailer(fail=True)
        sent_fail = await services.dispatch_notifications(db, smtp=_smtp(), mailer=bad)
        await db.commit()
        assert sent_fail == 0
        assert piece.last_notified_at is None

        # Second dispatch with a working mailer → goes through.
        good = _RecordingMailer()
        sent_ok = await services.dispatch_notifications(db, smtp=_smtp(), mailer=good)
        assert sent_ok == 1
        assert len(good.sent) == 1


async def test_dispatch_notifications_uses_override_email(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        u = User(
            email="login@example.com",
            password_hash="x",
            mailer_id=314159,
            notify_on_scans=True,
            notify_email="alerts+m@example.com",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        piece = await services.create_piece(
            db,
            store=store,
            user=u,
            draft=PieceDraft(
                recipient_block_inline="x",
                recipient_zip_inline="94105",
            ),
        )
        await services.ingest_scan(
            db,
            piece,
            {"scanDatetime": "2025-01-15T10:00", "scanEventCode": "SL"},
            source="poll",
        )
        await db.commit()
        mailer = _RecordingMailer()
        await services.dispatch_notifications(db, smtp=_smtp(), mailer=mailer)
        assert mailer.sent[0].to == "alerts+m@example.com"


async def test_dispatch_no_smtp_or_disabled_is_noop(
    db_sessionmaker: async_sessionmaker, store: Store
) -> None:
    async with db_sessionmaker() as db:
        sent = await services.dispatch_notifications(db, smtp=None)
        assert sent == 0


# ---------------------------------------------------------------------------
# Sheet allocator
# ---------------------------------------------------------------------------


def test_sheet_allocator_paginates_at_page_boundary() -> None:
    from mailtrace.routes.pieces import AVERY_8163_LAYOUT, _allocate_sheet

    # 25 fake pieces (just need objects with the right attributes).
    class _Stub:
        def __init__(self, n: int) -> None:
            self.id = n
            self.imb_letters = "A" * 65
            self.recipient_block = "x"

        def human_readable_imb(self) -> str:
            return f"id-{self.id}"

    pieces = [_Stub(i) for i in range(25)]
    pages = _allocate_sheet(pieces, layout=AVERY_8163_LAYOUT, start_row=1, start_col=1)  # type: ignore[arg-type]
    # 10 per page; 25 pieces → 3 pages.
    assert len(pages) == 3
    assert sum(len(p) for p in pages) == 25


def test_sheet_allocator_skips_used_cells_on_first_page() -> None:
    from mailtrace.routes.pieces import AVERY_8163_LAYOUT, _allocate_sheet

    class _Stub:
        def __init__(self, n: int) -> None:
            self.id = n
            self.imb_letters = ""
            self.recipient_block = ""

        def human_readable_imb(self) -> str:
            return ""

    pieces = [_Stub(0)]
    pages = _allocate_sheet(pieces, layout=AVERY_8163_LAYOUT, start_row=3, start_col=2)  # type: ignore[arg-type]
    cell = pages[0][0]
    # row 3 col 2: top = 0.5 + 2*2 = 4.5in, left = 0.2 + 1*4.25 = 4.45in
    assert abs(cell["top_in"] - 4.5) < 1e-6
    assert abs(cell["left_in"] - 4.45) < 1e-6
