"""Domain services that span more than one route or live outside the HTTP cycle.

In here:
- piece creation (allocate serial, encode IMb, snapshot addresses, store)
- scan ingestion (dedup + write) for both poll and push paths
- next-poll-at calculation
- the background poller's per-cycle work function
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import imb as imb_lib
from .mail import Mailer, MailerError, OutgoingMessage
from .models import (
    STATUS_DELIVERED,
    STATUS_GENERATED,
    STATUS_IN_FLIGHT,
    STATUS_PRINTED,
    Address,
    MailPiece,
    Scan,
    SmtpConfig,
    User,
    utcnow,
)
from .store import Store
from .usps import USPSClient, USPSError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Piece creation
# ---------------------------------------------------------------------------


@dataclass
class PieceDraft:
    """Inputs from the create / batch-create UI for one piece."""

    label: str = ""
    sender_address: Address | None = None
    recipient_address: Address | None = None
    sender_block_inline: str = ""
    recipient_block_inline: str = ""
    recipient_zip_inline: str = ""  # used when recipient_address is None
    include_zip_in_imb: bool = True


class PieceValidationError(ValueError):
    pass


def _zip_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _resolve_recipient_zip(draft: PieceDraft) -> str:
    if draft.recipient_address is not None:
        digits = _zip_digits(draft.recipient_address.zip)
    else:
        digits = _zip_digits(draft.recipient_zip_inline)
    if len(digits) not in (5, 9, 11):
        raise PieceValidationError("recipient ZIP must be 5, 9 or 11 digits")
    return digits


def _resolve_blocks(draft: PieceDraft) -> tuple[str, str]:
    sender = (
        draft.sender_address.to_recipient_block()
        if draft.sender_address is not None
        else draft.sender_block_inline.strip()
    )
    if draft.recipient_address is not None:
        recipient = draft.recipient_address.to_recipient_block()
    else:
        recipient = draft.recipient_block_inline.strip()
    if not recipient:
        raise PieceValidationError("recipient block cannot be empty")
    return sender, recipient


async def create_piece(
    db: AsyncSession,
    *,
    store: Store,
    user: User,
    draft: PieceDraft,
    initial_status: str = STATUS_GENERATED,
) -> MailPiece:
    if user.mailer_id is None:
        raise PieceValidationError(
            "set your USPS Mailer ID on the Account page before creating a piece"
        )
    if initial_status not in (STATUS_GENERATED, STATUS_PRINTED, STATUS_IN_FLIGHT):
        raise PieceValidationError(f"invalid initial status: {initial_status!r}")
    zip_digits = _resolve_recipient_zip(draft)
    sender_block, recipient_block = _resolve_blocks(draft)

    # Touch the picked addresses' last_used_at — useful in the UI to sort
    # the dropdowns by recency.
    now = utcnow()
    for picked in (draft.sender_address, draft.recipient_address):
        if picked is not None:
            picked.last_used_at = now

    serial = await store.next_serial()
    delivery_for_imb = zip_digits if draft.include_zip_in_imb else ""
    imb_letters = imb_lib.encode(
        user.barcode_id, user.service_type_id, user.mailer_id, serial, delivery_for_imb
    )
    imb_raw = imb_lib.to_raw_imb(
        user.barcode_id, user.service_type_id, user.mailer_id, serial, delivery_for_imb
    )

    printed_at = now if initial_status in (STATUS_PRINTED, STATUS_IN_FLIGHT) else None
    mailed_at = now if initial_status == STATUS_IN_FLIGHT else None
    next_poll_at = now if initial_status == STATUS_IN_FLIGHT else None

    piece = MailPiece(
        user_id=user.id,
        label=draft.label.strip()[:80],
        sender_address_id=draft.sender_address.id if draft.sender_address else None,
        recipient_address_id=draft.recipient_address.id if draft.recipient_address else None,
        sender_block=sender_block,
        recipient_block=recipient_block,
        recipient_zip_raw=zip_digits,
        barcode_id=user.barcode_id,
        service_type_id=user.service_type_id,
        mailer_id=user.mailer_id,
        serial=serial,
        include_zip_in_imb=draft.include_zip_in_imb,
        imb_letters=imb_letters,
        imb_raw=imb_raw,
        status=initial_status,
        printed_at=printed_at,
        mailed_at=mailed_at,
        next_poll_at=next_poll_at,
    )
    db.add(piece)
    await db.flush()  # populate piece.id without committing the outer txn
    return piece


# ---------------------------------------------------------------------------
# Scan ingestion (shared by poll + push paths)
# ---------------------------------------------------------------------------


_DELIVERY_MARKERS = {"01", "DELIVERED", "DLVD"}


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # tolerate trailing Z
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scan_dedup_hash(norm: dict[str, Any]) -> str:
    """Idempotency key for a scan. Built from the normalized fields, so
    the same physical scan event hashes the same regardless of which
    field-name variant USPS chose for that delivery."""
    parts = [
        str(norm.get("scan_date_time") or ""),
        str(norm.get("scan_event_code") or ""),
        str(norm.get("machine_name") or ""),
        str(norm.get("scan_facility_zip") or ""),
        str(norm.get("scan_facility_locale_key") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _flatten_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Lower-case keys and strip non-alphanumerics so payload field names
    match regardless of camelCase / snake_case / capitalization quirks
    (USPS' IV-MTR fields are user-selected in their portal and the casing
    isn't guaranteed)."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        flat = "".join(ch for ch in k.lower() if ch.isalnum())
        # Don't clobber an earlier hit; first-wins so an explicit
        # camelCase from the spec beats a stray duplicate.
        if flat not in out:
            out[flat] = v
    return out


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a USPS scan event (on-demand piece API, IV push feed, or
    any of the field-name variants the IV portal can emit) into the column
    shape we store."""
    f = _flatten_keys(raw)
    return {
        "scan_date_time": _first(f, "scandatetime", "scandate", "eventdatetime"),
        "scan_event_code": _first(f, "scaneventcode", "eventcode") or "",
        "handling_event_type": _first(f, "handlingeventtype", "handlingevent") or "",
        "mail_phase": _first(f, "mailphase") or "",
        "machine_name": _first(f, "machinename", "machineid") or "",
        "scanner_type": _first(f, "scannertype") or "",
        "scan_facility_name": _first(f, "scanfacilityname", "facilityname") or "",
        "scan_facility_locale_key": _first(f, "scanlocalekey", "localekey") or "",
        "scan_facility_city": _first(f, "scanfacilitycity", "facilitycity") or "",
        "scan_facility_state": _first(f, "scanfacilitystate", "facilitystate") or "",
        "scan_facility_zip": _first(f, "scanfacilityzip", "facilityzip") or "",
    }


def imb_from_event(raw: dict[str, Any]) -> str | None:
    """Pull the IMb out of a scan event under any of its name variants."""
    f = _flatten_keys(raw)
    v = _first(f, "imb", "imbarcode", "intelligentmailbarcode", "imbcode")
    return v if isinstance(v, str) and v else None


async def ingest_scan(
    db: AsyncSession, piece: MailPiece, raw_event: dict[str, Any], *, source: str
) -> bool:
    """Insert a normalized scan; return True iff a new row was added."""
    norm = _normalize_event(raw_event)
    dedup = _scan_dedup_hash(norm)
    scan = Scan(
        mailpiece_id=piece.id,
        source=source,
        scanned_at=_parse_iso(norm["scan_date_time"]),
        event_code=norm["scan_event_code"][:16],
        handling_event_type=norm["handling_event_type"][:8],
        mail_phase=norm["mail_phase"][:80],
        machine_name=norm["machine_name"][:80],
        scanner_type=norm["scanner_type"][:40],
        facility_name=norm["scan_facility_name"][:160],
        facility_locale_key=norm["scan_facility_locale_key"][:40],
        facility_city=norm["scan_facility_city"][:120],
        facility_state=norm["scan_facility_state"][:2],
        facility_zip=norm["scan_facility_zip"][:11],
        dedup_hash=dedup,
        raw_payload=json.dumps(raw_event, default=str)[:4000],
    )
    db.add(scan)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return False

    # A scan landing on a stock piece means USPS already has it — promote
    # it into the polling lifecycle so the user sees subsequent updates.
    if piece.status in (STATUS_GENERATED, STATUS_PRINTED):
        piece.status = STATUS_IN_FLIGHT
        piece.mailed_at = piece.mailed_at or utcnow()
        piece.next_poll_at = utcnow()

    # If this scan looks like a delivery event, mark the piece delivered.
    code = (norm["scan_event_code"] or "").upper()
    if any(marker in code for marker in _DELIVERY_MARKERS):
        piece.status = STATUS_DELIVERED
    return True


async def ingest_piece_payload(db: AsyncSession, piece: MailPiece, payload: dict[str, Any]) -> int:
    """Drain a USPS piece-tracking JSON payload into Scan rows.

    Returns the number of new scans persisted.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 0
    scans = data.get("scans") or []
    inserted = 0
    for raw in scans:
        if not isinstance(raw, dict):
            continue
        if await ingest_scan(db, piece, raw, source="poll"):
            inserted += 1
    # Heuristic: if USPS hands us an actual_delivery_date (or similar field),
    # promote the piece to "delivered" even if the scan codes didn't trip
    # the marker check above.
    for key in ("actualDeliveryDate", "actual_delivery_date", "deliveryDate", "delivery_date"):
        if data.get(key):
            piece.status = STATUS_DELIVERED
            break
    return inserted


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def _ensure_aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite round-trips DateTime(timezone=True) as naive UTC. Normalize."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def next_poll_at_for(piece: MailPiece, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """When should this piece next be polled?

    None means "never again" (terminal / archived).
    """
    if piece.status != STATUS_IN_FLIGHT:
        return None
    now = now or utcnow()
    created = _ensure_aware(piece.created_at)
    age = now - created if created is not None else dt.timedelta()
    if piece.consecutive_poll_errors > 0:
        # Exponential backoff capped at 6h.
        seconds = min(60 * 30 * (2 ** (piece.consecutive_poll_errors - 1)), 6 * 3600)
        return now + dt.timedelta(seconds=seconds)
    # Cadence based on age. We deliberately don't peek at scans here — that
    # would force a relationship load in async contexts and the simple
    # age-based rule has been good enough in practice.
    if age < dt.timedelta(hours=1):
        return now + dt.timedelta(minutes=15)
    if age < dt.timedelta(days=2):
        return now + dt.timedelta(minutes=30)
    if age < dt.timedelta(days=7):
        return now + dt.timedelta(hours=2)
    return now + dt.timedelta(hours=6)


async def poll_one(
    db: AsyncSession, *, piece: MailPiece, usps: USPSClient, user: User | None = None
) -> tuple[int, str | None]:
    """Pull tracking for a single piece using the piece owner's BCG creds.

    `user` is optional purely so the per-piece manual-refresh route can
    pass the already-loaded `request.state.user` instead of round-tripping
    to load it again. The poller passes the piece's owner explicitly.
    """
    if user is None:
        user = await db.get(User, piece.user_id)
    if user is None:
        piece.consecutive_poll_errors += 1
        piece.last_polled_at = utcnow()
        piece.next_poll_at = next_poll_at_for(piece)
        return 0, "owning user not found"
    try:
        payload = await usps.get_piece_tracking(user, piece.imb_raw)
    except USPSError as err:
        piece.consecutive_poll_errors += 1
        piece.last_polled_at = utcnow()
        piece.next_poll_at = next_poll_at_for(piece)
        return 0, str(err)
    inserted = await ingest_piece_payload(db, piece, payload)
    piece.consecutive_poll_errors = 0
    piece.last_polled_at = utcnow()
    piece.next_poll_at = next_poll_at_for(piece)
    return inserted, None


async def select_due_pieces(
    db: AsyncSession, *, limit: int, now: dt.datetime | None = None
) -> list[MailPiece]:
    now = now or utcnow()
    stmt = (
        select(MailPiece)
        .where(
            MailPiece.status == STATUS_IN_FLIGHT,
            MailPiece.archived_at.is_(None),
            (MailPiece.next_poll_at.is_(None)) | (MailPiece.next_poll_at <= now),
        )
        .order_by(MailPiece.next_poll_at.asc().nulls_first())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def auto_archive_stale(db: AsyncSession, *, days: int, now: dt.datetime | None = None) -> int:
    """Soft-archive in-flight pieces that have been quiet for too long."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=days)
    pieces = list(
        (
            await db.execute(
                select(MailPiece).where(
                    MailPiece.status == STATUS_IN_FLIGHT,
                    MailPiece.archived_at.is_(None),
                    MailPiece.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for p in pieces:
        p.archived_at = now
    return len(pieces)


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------


def _ensure_aware_or(value: dt.datetime | None, fallback: dt.datetime) -> dt.datetime:
    if value is None:
        return fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _format_scan_line(scan: Scan) -> str:
    when = scan.scanned_at.strftime("%Y-%m-%d %H:%M") if scan.scanned_at else "—"
    where_parts = [
        scan.facility_name or "",
        scan.facility_city or "",
        scan.facility_state or "",
    ]
    where = ", ".join(p for p in where_parts if p) or "(facility unknown)"
    label = scan.event_code or scan.handling_event_type or "scan"
    return f"  {when}  {label}  · {where}"


def _build_digest(
    user: User,
    pieces_with_new_scans: list[tuple[MailPiece, list[Scan]]],
    public_base_url: str,
) -> OutgoingMessage:
    total = sum(len(s) for _, s in pieces_with_new_scans)
    plural = "" if total == 1 else "s"
    subject = f"mailtrace: {total} new scan{plural} on {len(pieces_with_new_scans)} piece(s)"
    lines = [f"{total} new scan{plural} across {len(pieces_with_new_scans)} piece(s):", ""]
    html_blocks = [f"<p>{total} new scan{plural} across {len(pieces_with_new_scans)} piece(s):</p>"]
    for piece, scans in pieces_with_new_scans:
        title = piece.label or f"piece #{piece.id}"
        url = f"{public_base_url.rstrip('/')}/pieces/{piece.id}" if public_base_url else ""
        lines.append(f'"{title}"  ({piece.human_readable_imb()}):')
        for s in scans:
            lines.append(_format_scan_line(s))
        if url:
            lines.append(f"  → {url}")
        lines.append("")

        html_blocks.append(f"<h3>{title} <small>{piece.human_readable_imb()}</small></h3>")
        html_blocks.append("<table border='1' cellpadding='4' style='border-collapse:collapse'>")
        html_blocks.append(
            "<tr><th>When</th><th>Event</th><th>Facility</th><th>City</th><th>State</th></tr>"
        )
        for s in scans:
            when = s.scanned_at.strftime("%Y-%m-%d %H:%M") if s.scanned_at else "—"
            label = s.event_code or s.handling_event_type or "—"
            html_blocks.append(
                f"<tr><td>{when}</td><td>{label}</td>"
                f"<td>{s.facility_name or '—'}</td>"
                f"<td>{s.facility_city or '—'}</td>"
                f"<td>{s.facility_state or '—'}</td></tr>"
            )
        html_blocks.append("</table>")
        if url:
            html_blocks.append(f'<p><a href="{url}">Open piece →</a></p>')

    return OutgoingMessage(
        to=user.notify_email or user.email,
        subject=subject,
        body_text="\n".join(lines),
        body_html="<html><body>" + "".join(html_blocks) + "</body></html>",
    )


async def dispatch_notifications(
    db: AsyncSession,
    *,
    smtp: SmtpConfig | None,
    mailer: Mailer | None = None,
) -> int:
    """Find users with new unnotified scans and send digest emails.

    Returns the number of digests sent. Per-piece `last_notified_at` is
    only bumped on successful delivery.
    """
    if smtp is None or not smtp.enabled:
        return 0

    # Load every (user, piece) where there are scans newer than the piece's
    # last_notified_at and the user has notifications on. We only look at
    # users with notify_on_scans=True.
    stmt = (
        select(MailPiece)
        .join(User, MailPiece.user_id == User.id)
        .where(User.notify_on_scans.is_(True), MailPiece.archived_at.is_(None))
    )
    pieces = list((await db.execute(stmt)).scalars().all())
    if not pieces:
        return 0

    # Bucket pieces by user and gather scans per piece.
    by_user: dict[int, list[tuple[MailPiece, list[Scan]]]] = {}
    user_cache: dict[int, User] = {}
    for piece in pieces:
        cutoff = _ensure_aware_or(piece.last_notified_at, dt.datetime.min.replace(tzinfo=dt.UTC))
        scan_stmt = (
            select(Scan)
            .where(Scan.mailpiece_id == piece.id, Scan.created_at > cutoff)
            .order_by(Scan.scanned_at.asc().nulls_last())
        )
        new_scans = list((await db.execute(scan_stmt)).scalars().all())
        if not new_scans:
            continue
        if piece.user_id not in user_cache:
            owner = await db.get(User, piece.user_id)
            if owner is None:
                continue
            user_cache[piece.user_id] = owner
        by_user.setdefault(piece.user_id, []).append((piece, new_scans))

    sent = 0
    if mailer is None:
        mailer = Mailer(smtp)
    now = utcnow()
    for user_id, items in by_user.items():
        owner = user_cache[user_id]
        msg = _build_digest(owner, items, smtp.public_base_url)
        try:
            await mailer.send(msg)
        except MailerError as err:
            log.warning("notification dispatch failed for %s: %s", owner.email, err)
            continue
        for piece, _ in items:
            piece.last_notified_at = now
        sent += 1
    return sent
