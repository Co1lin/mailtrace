"""Mail piece routes: list, create (single + batch), detail, archive, delete."""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import pdf, services
from ..auth import CurrentUserDep
from ..db import SessionDep
from ..models import (
    STATUS_ARCHIVED,
    STATUS_GENERATED,
    STATUS_IN_FLIGHT,
    STATUS_PRINTED,
    Address,
    MailPiece,
    utcnow,
)
from ..services import PieceDraft, PieceValidationError

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user_addresses(db: AsyncSession, user_id: int) -> list[Address]:
    rows = (
        (
            await db.execute(
                select(Address).where(Address.user_id == user_id).order_by(Address.label)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _load_owned(db: AsyncSession, user_id: int, piece_id: int) -> MailPiece:
    piece = (
        await db.execute(
            select(MailPiece).options(selectinload(MailPiece.scans)).where(MailPiece.id == piece_id)
        )
    ).scalar_one_or_none()
    if piece is None or piece.user_id != user_id:
        raise HTTPException(status_code=404, detail="piece not found")
    return piece


async def _load_address_or_none(
    db: AsyncSession, user_id: int, address_id: int | None
) -> Address | None:
    if address_id is None:
        return None
    addr = await db.get(Address, address_id)
    if addr is None or addr.user_id != user_id:
        raise HTTPException(status_code=400, detail=f"address {address_id} not found")
    return addr


def _parse_optional_int(value: str) -> int | None:
    s = value.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=f"expected an integer, got {value!r}") from err


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


_LIST_STATUS_FILTERS = {
    "generated": STATUS_GENERATED,
    "printed": STATUS_PRINTED,
    "in_flight": STATUS_IN_FLIGHT,
    "delivered": "delivered",
}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def list_pieces(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
    archived: int = 0,
    status: str = "",
) -> HTMLResponse:
    show_archived = bool(archived)
    status_filter = status.strip().lower()
    if status_filter and status_filter not in _LIST_STATUS_FILTERS:
        raise HTTPException(status_code=400, detail=f"unknown status filter: {status_filter!r}")

    # id desc as a tiebreaker: batch-creates land in the same created_at
    # second, and we want the latest-inserted piece on top of its siblings,
    # not the first one (which is what the DB returns by insertion order).
    stmt = (
        select(MailPiece)
        .where(MailPiece.user_id == user.id)
        .order_by(MailPiece.created_at.desc(), MailPiece.id.desc())
    )
    if show_archived:
        stmt = stmt.where(MailPiece.archived_at.is_not(None))
    else:
        stmt = stmt.where(MailPiece.archived_at.is_(None))
    if status_filter:
        stmt = stmt.where(MailPiece.status == _LIST_STATUS_FILTERS[status_filter])
    pieces = list((await db.execute(stmt)).scalars().all())

    counts_stmt = select(
        func.count().filter(MailPiece.archived_at.is_(None)),
        func.count().filter(MailPiece.archived_at.is_not(None)),
        func.count().filter(MailPiece.archived_at.is_(None), MailPiece.status == STATUS_GENERATED),
        func.count().filter(MailPiece.archived_at.is_(None), MailPiece.status == STATUS_PRINTED),
        func.count().filter(MailPiece.archived_at.is_(None), MailPiece.status == STATUS_IN_FLIGHT),
        func.count().filter(MailPiece.archived_at.is_(None), MailPiece.status == "delivered"),
    ).where(MailPiece.user_id == user.id)
    (
        active_count,
        archived_count,
        generated_count,
        printed_count,
        in_flight_count,
        delivered_count,
    ) = (await db.execute(counts_stmt)).one()

    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/list.html",
        {
            "pieces": pieces,
            "user": user,
            "show_archived": show_archived,
            "status_filter": status_filter,
            "active_count": active_count,
            "archived_count": archived_count,
            "generated_count": generated_count,
            "printed_count": printed_count,
            "in_flight_count": in_flight_count,
            "delivered_count": delivered_count,
        },
    )
    return response


# ---------------------------------------------------------------------------
# Create (single)
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
async def new_piece_form(request: Request, db: SessionDep, user: CurrentUserDep) -> HTMLResponse:
    addresses = await _user_addresses(db, user.id)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/new.html",
        {"user": user, "addresses": addresses, "error": None},
    )
    return response


@router.post("/new")
async def create_one(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
    label: Annotated[str, Form()] = "",
    sender_address_id: Annotated[str, Form()] = "",
    sender_block_inline: Annotated[str, Form()] = "",
    recipient_address_id: Annotated[str, Form()] = "",
    recipient_name: Annotated[str, Form()] = "",
    recipient_company: Annotated[str, Form()] = "",
    recipient_street: Annotated[str, Form()] = "",
    recipient_address2: Annotated[str, Form()] = "",
    recipient_city: Annotated[str, Form()] = "",
    recipient_state: Annotated[str, Form()] = "",
    recipient_zip: Annotated[str, Form()] = "",
    # Default False: an unchecked checkbox is omitted from the form payload.
    # The HTML defaults to `checked`, so users opt in by default but can
    # opt out for the ZIP-less-IMb experiment.
    include_zip_in_imb: Annotated[bool, Form()] = False,
    # The single-piece form is for "create one and mail it now" — default
    # to in_flight (preserves prior behavior). Users who want a stock
    # piece they'll print later set keep_as_stock=True.
    keep_as_stock: Annotated[bool, Form()] = False,
) -> Response:
    sender = await _load_address_or_none(db, user.id, _parse_optional_int(sender_address_id))
    recipient = await _load_address_or_none(db, user.id, _parse_optional_int(recipient_address_id))
    inline_recipient = ""
    if recipient is None:
        # Inline recipient: build the block from the discrete fields.
        zip_full = recipient_zip.strip()
        digits = "".join(ch for ch in zip_full if ch.isdigit())
        if len(digits) >= 9:
            zip_part = f"{digits[:5]}-{digits[5:9]}"
            if len(digits) == 11:
                zip_part += f"-{digits[9:]}"
        else:
            zip_part = digits
        parts = [
            recipient_name.strip(),
            recipient_company.strip(),
            recipient_street.strip(),
            recipient_address2.strip(),
            f"{recipient_city.strip()}, {recipient_state.strip()}, {zip_part}".strip(", "),
        ]
        inline_recipient = "\n".join(p for p in parts if p)

    draft = PieceDraft(
        label=label,
        sender_address=sender,
        recipient_address=recipient,
        sender_block_inline=sender_block_inline,
        recipient_block_inline=inline_recipient,
        recipient_zip_inline=recipient_zip,
        include_zip_in_imb=bool(include_zip_in_imb),
    )

    store = request.app.state.store
    initial_status = STATUS_GENERATED if keep_as_stock else STATUS_IN_FLIGHT
    try:
        piece = await services.create_piece(
            db, store=store, user=user, draft=draft, initial_status=initial_status
        )
    except PieceValidationError as err:
        addresses = await _user_addresses(db, user.id)
        templates = request.app.state.templates
        resp: Response = templates.TemplateResponse(
            request,
            "pieces/new.html",
            {"user": user, "addresses": addresses, "error": str(err)},
            status_code=400,
        )
        return resp
    await db.commit()
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


# ---------------------------------------------------------------------------
# Create (batch)
# ---------------------------------------------------------------------------


_BATCH_COUNT_LIMIT = 50  # per-row safety so a typo can't generate 10k pieces
_BATCH_TOTAL_LIMIT = 500  # cumulative cap across all rows in one submission
_BATCH_INITIAL_ROWS = 6  # rendered on first page load (user can "+ Add row")


@router.get("/batch", response_class=HTMLResponse)
async def batch_form(request: Request, db: SessionDep, user: CurrentUserDep) -> HTMLResponse:
    addresses = await _user_addresses(db, user.id)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/batch.html",
        {
            "user": user,
            "addresses": addresses,
            "error": None,
            "row_count": _BATCH_INITIAL_ROWS,
            "default_count": 1,
            "batch_count_limit": _BATCH_COUNT_LIMIT,
            "batch_total_limit": _BATCH_TOTAL_LIMIT,
        },
    )
    return response


@router.post("/batch")
async def batch_create(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
) -> Response:
    form = await request.form()
    rows: list[dict[str, str]] = []
    # form keys look like row-0-sender_id, row-0-recipient_id, etc.
    indices = sorted({int(k.split("-")[1]) for k in form if k.startswith("row-") and "-" in k})
    for i in indices:
        rows.append(
            {
                "label": str(form.get(f"row-{i}-label", "") or ""),
                "sender_id": str(form.get(f"row-{i}-sender_id", "") or ""),
                "recipient_id": str(form.get(f"row-{i}-recipient_id", "") or ""),
                "include_zip": str(form.get(f"row-{i}-include_zip", "") or ""),
                "count": str(form.get(f"row-{i}-count", "") or ""),
            }
        )

    # Global default: applied to any row whose Count is left blank. Per-row
    # Count overrides this (use 0 to skip, or a higher number for extras).
    default_count_raw = str(form.get("default_count", "") or "").strip() or "1"
    try:
        default_count = int(default_count_raw)
    except ValueError:
        default_count = 1
    if default_count < 0:
        default_count = 0
    if default_count > _BATCH_COUNT_LIMIT:
        default_count = _BATCH_COUNT_LIMIT

    # Form-level toggle: default behavior is "stock" (generate IMbs for
    # later printing/mailing). When checked, mark all created pieces as
    # already mailed (start polling immediately).
    mark_as_mailed = str(form.get("mark_as_mailed", "")).lower() in ("on", "true", "1")
    initial_status = STATUS_IN_FLIGHT if mark_as_mailed else STATUS_GENERATED

    # Pre-resolve each row into a "plan" tuple before any DB writes so we
    # can interleave creation order: A,B,C,A,B,C instead of A,A,A,B,B,C.
    # Skips blank-recipient rows silently; collects per-row errors otherwise.
    errors: list[str] = []
    plan: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not row["recipient_id"]:
            continue  # blank row: skip silently
        # Per-row count: blank → use default; explicit 0 → skip this row.
        raw = row["count"].strip()
        if not raw:
            count = default_count
        else:
            try:
                count = int(raw)
            except ValueError:
                errors.append(f"row {i + 1}: count must be an integer")
                continue
        if count <= 0:
            continue
        if count > _BATCH_COUNT_LIMIT:
            errors.append(f"row {i + 1}: count {count} exceeds per-row limit {_BATCH_COUNT_LIMIT}")
            continue
        try:
            sender = await _load_address_or_none(db, user.id, _parse_optional_int(row["sender_id"]))
            recipient = await _load_address_or_none(
                db, user.id, _parse_optional_int(row["recipient_id"])
            )
        except HTTPException as err:
            errors.append(f"row {i + 1}: {err.detail}")
            continue
        if recipient is None:
            errors.append(f"row {i + 1}: recipient required")
            continue
        plan.append(
            {
                "row_index": i,
                "count": count,
                "label": row["label"],
                "sender": sender,
                "recipient": recipient,
                "include_zip": row["include_zip"] in ("on", "true", "1"),
            }
        )

    total = sum(p["count"] for p in plan)
    if total > _BATCH_TOTAL_LIMIT:
        errors.append(f"total of {total} pieces exceeds per-submission limit {_BATCH_TOTAL_LIMIT}")
        plan = []  # refuse the whole submission rather than partially creating

    # Round-robin interleave: round 0 emits one piece per row that wants ≥1
    # copy, round 1 emits one per row that wants ≥2, … through max(counts).
    # Rows with smaller counts naturally drop out as rounds advance.
    created: list[MailPiece] = []
    store = request.app.state.store
    max_count = max((p["count"] for p in plan), default=0)
    for round_idx in range(max_count):
        for entry in plan:
            if round_idx >= entry["count"]:
                continue
            draft = PieceDraft(
                label=entry["label"],
                sender_address=entry["sender"],
                recipient_address=entry["recipient"],
                include_zip_in_imb=entry["include_zip"],
            )
            try:
                piece = await services.create_piece(
                    db,
                    store=store,
                    user=user,
                    draft=draft,
                    initial_status=initial_status,
                )
            except (PieceValidationError, HTTPException) as err:
                detail = err.detail if isinstance(err, HTTPException) else str(err)
                errors.append(f"row {entry['row_index'] + 1}: {detail}")
                continue
            created.append(piece)

    if errors and not created:
        addresses = await _user_addresses(db, user.id)
        templates = request.app.state.templates
        resp: Response = templates.TemplateResponse(
            request,
            "pieces/batch.html",
            {
                "user": user,
                "addresses": addresses,
                "error": " · ".join(errors),
                "row_count": max(len(rows), _BATCH_INITIAL_ROWS),
                "default_count": default_count,
                "batch_count_limit": _BATCH_COUNT_LIMIT,
                "batch_total_limit": _BATCH_TOTAL_LIMIT,
            },
            status_code=400,
        )
        return resp

    await db.commit()
    request.session["batch_created_count"] = len(created)
    if errors:
        request.session["batch_errors"] = errors
    return RedirectResponse("/pieces/", status_code=303)


# ---------------------------------------------------------------------------
# CSV bulk import
# ---------------------------------------------------------------------------


_CSV_RECOGNIZED_FIELDS = {
    "label",
    "name",
    "company",
    "street",
    "address2",
    "city",
    "state",
    "zip",
    "include_zip_in_imb",
}
_CSV_REQUIRED_FIELDS = ("street", "city", "state", "zip")


@router.get("/import", response_class=HTMLResponse)
async def import_form(request: Request, db: SessionDep, user: CurrentUserDep) -> HTMLResponse:
    addresses = await _user_addresses(db, user.id)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/import.html",
        {
            "user": user,
            "addresses": addresses,
            "error": None,
            "results": None,
            "recognized_fields": sorted(_CSV_RECOGNIZED_FIELDS),
            "required_fields": _CSV_REQUIRED_FIELDS,
        },
    )
    return response


@router.post("/import")
async def import_csv(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
    csv_text: Annotated[str, Form()] = "",
    sender_address_id: Annotated[str, Form()] = "",
    save_addresses: Annotated[bool, Form()] = False,
    include_zip_in_imb: Annotated[bool, Form()] = False,
    mark_as_mailed: Annotated[bool, Form()] = False,
) -> Response:
    text = csv_text.strip()
    if not text:
        return _import_render_error(request, db, user, "Paste at least one row of CSV.")

    sender = await _load_address_or_none(db, user.id, _parse_optional_int(sender_address_id))

    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as err:
        return _import_render_error(request, db, user, f"CSV parse error: {err}")

    headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    missing = [f for f in _CSV_REQUIRED_FIELDS if f not in headers]
    if missing:
        return _import_render_error(
            request,
            db,
            user,
            f"CSV is missing required columns: {', '.join(missing)}",
        )
    unknown = headers - _CSV_RECOGNIZED_FIELDS
    # Don't error on unknown columns — just ignore them — but note them.

    store = request.app.state.store
    errors: list[str] = []
    created_pieces: list[MailPiece] = []
    saved_addresses = 0
    for line_no, row in enumerate(reader, start=2):  # line 1 is header
        normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            label = normalized.get("label", "")
            name = normalized.get("name", "")
            company = normalized.get("company", "")
            street = normalized.get("street", "")
            address2 = normalized.get("address2", "")
            city = normalized.get("city", "")
            state = normalized.get("state", "").upper()
            zip_raw = normalized.get("zip", "")
            row_value = normalized.get("include_zip_in_imb", "")
            if row_value:
                row_include_zip = row_value.lower() in ("1", "true", "yes", "y")
            else:
                row_include_zip = include_zip_in_imb
            if not (street and city and state and zip_raw):
                # Skip silently empty rows; complain about partials.
                if any((street, city, state, zip_raw)):
                    errors.append(f"line {line_no}: missing one of street/city/state/zip")
                continue

            recipient: Address | None = None
            if save_addresses:
                # Use label as the address-book label; if blank, synthesize.
                book_label = label or f"csv-{name or street}"[:80]
                # Make it unique among the user's labels by suffixing if needed.
                book_label = await _unique_label(db, user.id, book_label)
                recipient = Address(
                    user_id=user.id,
                    label=book_label,
                    role="recipient",
                    name=name,
                    company=company,
                    street=street,
                    address2=address2,
                    city=city,
                    state=state,
                    zip="".join(ch for ch in zip_raw if ch.isdigit() or ch == "-"),
                )
                db.add(recipient)
                await db.flush()
                saved_addresses += 1

            inline_recipient = ""
            if recipient is None:
                digits = "".join(ch for ch in zip_raw if ch.isdigit())
                if len(digits) >= 9:
                    zip_part = f"{digits[:5]}-{digits[5:9]}"
                    if len(digits) == 11:
                        zip_part += f"-{digits[9:]}"
                else:
                    zip_part = digits
                parts = [
                    name,
                    company,
                    street,
                    address2,
                    f"{city}, {state}, {zip_part}".strip(", "),
                ]
                inline_recipient = "\n".join(p for p in parts if p)

            draft = PieceDraft(
                label=label,
                sender_address=sender,
                recipient_address=recipient,
                recipient_block_inline=inline_recipient,
                recipient_zip_inline=zip_raw,
                include_zip_in_imb=row_include_zip,
            )
            piece = await services.create_piece(
                db,
                store=store,
                user=user,
                draft=draft,
                initial_status=STATUS_IN_FLIGHT if mark_as_mailed else STATUS_GENERATED,
            )
            created_pieces.append(piece)
        except (PieceValidationError, ValueError) as err:
            errors.append(f"line {line_no}: {err}")

    if not created_pieces and errors:
        await db.rollback()
        return _import_render_error(
            request, db, user, "No pieces created. Errors:\n" + "\n".join(errors)
        )

    await db.commit()
    addresses = await _user_addresses(db, user.id)
    response: Response = request.app.state.templates.TemplateResponse(
        request,
        "pieces/import.html",
        {
            "user": user,
            "addresses": addresses,
            "error": None,
            "results": {
                "created": len(created_pieces),
                "saved_addresses": saved_addresses,
                "errors": errors,
                "unknown_columns": sorted(unknown),
            },
            "recognized_fields": sorted(_CSV_RECOGNIZED_FIELDS),
            "required_fields": _CSV_REQUIRED_FIELDS,
        },
    )
    return response


async def _unique_label(db: AsyncSession, user_id: int, base: str) -> str:
    """Find a label that doesn't collide with the user's existing addresses."""
    base = base.strip()[:70] or "csv"
    existing: set[str] = set(
        (await db.execute(select(Address.label).where(Address.user_id == user_id))).scalars().all()
    )
    if base not in existing:
        return base
    for n in range(2, 1000):
        candidate = f"{base} ({n})"[:80]
        if candidate not in existing:
            return candidate
    raise PieceValidationError(f"too many addresses with label {base!r}")


def _import_render_error(request: Request, db: SessionDep, user: Any, message: str) -> Response:
    # Re-render the import form with the error displayed. We don't load
    # addresses on the error path to keep failure cheap.
    resp: Response = request.app.state.templates.TemplateResponse(
        request,
        "pieces/import.html",
        {
            "user": user,
            "addresses": [],
            "error": message,
            "results": None,
            "recognized_fields": sorted(_CSV_RECOGNIZED_FIELDS),
            "required_fields": _CSV_REQUIRED_FIELDS,
        },
        status_code=400,
    )
    return resp


# ---------------------------------------------------------------------------
# Detail / actions
# ---------------------------------------------------------------------------


@router.get("/{piece_id}", response_class=HTMLResponse)
async def piece_detail(
    request: Request, piece_id: int, db: SessionDep, user: CurrentUserDep
) -> HTMLResponse:
    piece = await _load_owned(db, user.id, piece_id)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/detail.html",
        {"piece": piece, "user": user},
    )
    return response


@router.post("/{piece_id}/refresh")
async def piece_refresh(
    request: Request, piece_id: int, db: SessionDep, user: CurrentUserDep
) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    inserted, err = await services.poll_one(db, piece=piece, usps=request.app.state.usps, user=user)
    await db.commit()
    request.session["piece_flash"] = (
        f"refresh failed: {err}" if err else f"refresh ok ({inserted} new scan(s))"
    )
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


@router.post("/{piece_id}/archive")
async def piece_archive(piece_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    if piece.archived_at is None:
        _archive_in_place(piece)
        await db.commit()
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


@router.post("/{piece_id}/unarchive")
async def piece_unarchive(piece_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    if piece.archived_at is not None:
        _unarchive_in_place(piece)
        await db.commit()
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


@router.post("/{piece_id}/mark-printed")
async def piece_mark_printed(piece_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    _mark_printed_in_place(piece)
    await db.commit()
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


@router.post("/{piece_id}/mark-mailed")
async def piece_mark_mailed(piece_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    _mark_mailed_in_place(piece)
    await db.commit()
    return RedirectResponse(f"/pieces/{piece.id}", status_code=303)


def _archive_in_place(piece: MailPiece) -> None:
    if piece.archived_at is not None:
        return
    if piece.status != STATUS_ARCHIVED:
        piece.pre_archive_status = piece.status
    piece.archived_at = utcnow()
    piece.status = STATUS_ARCHIVED
    piece.next_poll_at = None


def _unarchive_in_place(piece: MailPiece) -> None:
    if piece.archived_at is None:
        return
    restore = piece.pre_archive_status or STATUS_GENERATED
    piece.archived_at = None
    piece.status = restore
    piece.pre_archive_status = ""
    piece.next_poll_at = utcnow() if restore == STATUS_IN_FLIGHT else None


def _mark_printed_in_place(piece: MailPiece) -> None:
    if piece.archived_at is not None:
        raise HTTPException(status_code=400, detail="cannot mark archived piece as printed")
    if piece.status not in (STATUS_GENERATED, STATUS_PRINTED):
        # Already mailed/delivered — silently ignore (clicking the same
        # button twice on a stale page shouldn't 400).
        return
    if piece.printed_at is None:
        piece.printed_at = utcnow()
    piece.status = STATUS_PRINTED


def _mark_mailed_in_place(piece: MailPiece) -> None:
    if piece.archived_at is not None:
        raise HTTPException(status_code=400, detail="cannot mark archived piece as mailed")
    if piece.status in (STATUS_IN_FLIGHT, "delivered"):
        return  # already past this state
    now = utcnow()
    if piece.printed_at is None:
        piece.printed_at = now
    piece.mailed_at = piece.mailed_at or now
    piece.status = STATUS_IN_FLIGHT
    piece.next_poll_at = now


@router.post("/{piece_id}/delete")
async def piece_delete(piece_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    await db.delete(piece)
    await db.commit()
    return RedirectResponse("/pieces/", status_code=303)


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


@router.post("/bulk-action")
async def bulk_action(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
) -> Response:
    form = await request.form()
    action = str(form.get("action", "") or "")
    raw_ids = form.getlist("ids") if hasattr(form, "getlist") else []
    try:
        ids = [int(x) for x in raw_ids if isinstance(x, str)]
    except ValueError as err:
        raise HTTPException(status_code=400, detail="invalid ids") from err
    if not ids:
        return RedirectResponse("/pieces/", status_code=303)

    pieces = list(
        (
            await db.execute(
                select(MailPiece).where(MailPiece.id.in_(ids), MailPiece.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    if action == "archive":
        for p in pieces:
            _archive_in_place(p)
    elif action == "unarchive":
        for p in pieces:
            _unarchive_in_place(p)
    elif action == "mark_printed":
        for p in pieces:
            _mark_printed_in_place(p)
    elif action == "mark_mailed":
        for p in pieces:
            _mark_mailed_in_place(p)
    elif action == "delete":
        for p in pieces:
            await db.delete(p)
    else:
        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
    await db.commit()
    return RedirectResponse(
        "/pieces/" + ("?archived=1" if action == "unarchive" else ""), status_code=303
    )


# ---------------------------------------------------------------------------
# Downloads (per-piece)
# ---------------------------------------------------------------------------


@router.get("/{piece_id}/download/envelope.{ext}")
async def download_envelope(
    request: Request,
    piece_id: int,
    ext: str,
    db: SessionDep,
    user: CurrentUserDep,
) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    return _render_piece_doc(request, piece, "envelope", ext)


@router.get("/sheet/setup", response_class=HTMLResponse)
async def sheet_setup(request: Request, db: SessionDep, user: CurrentUserDep) -> HTMLResponse:
    pieces = list(
        (
            await db.execute(
                select(MailPiece)
                .where(MailPiece.user_id == user.id, MailPiece.archived_at.is_(None))
                .order_by(MailPiece.created_at.desc(), MailPiece.id.desc())
            )
        )
        .scalars()
        .all()
    )
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "pieces/sheet_setup.html",
        {"user": user, "pieces": pieces, "layouts": AVERY_LAYOUTS},
    )
    return response


@router.post("/sheet")
async def sheet_render(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
) -> Response:
    form = await request.form()
    raw_ids = form.getlist("ids") if hasattr(form, "getlist") else []
    try:
        ids = [int(x) for x in raw_ids if isinstance(x, str)]
    except ValueError as err:
        raise HTTPException(status_code=400, detail="invalid ids") from err
    if not ids:
        raise HTTPException(status_code=400, detail="select at least one piece")

    try:
        start_row = int(str(form.get("start_row", "1")))
        start_col = int(str(form.get("start_col", "1")))
    except ValueError as err:
        raise HTTPException(status_code=400, detail="row/col must be integers") from err
    doc_type = str(form.get("doc_type", "pdf"))
    if doc_type not in ("pdf", "html"):
        raise HTTPException(status_code=400, detail="doc_type must be pdf or html")

    layout = resolve_layout(str(form.get("layout", "5163")))
    if not (1 <= start_row <= layout["rows"] and 1 <= start_col <= layout["cols"]):
        raise HTTPException(status_code=400, detail="start row/col out of range")

    pieces_by_id = {
        p.id: p
        for p in (
            await db.execute(
                select(MailPiece).where(MailPiece.id.in_(ids), MailPiece.user_id == user.id)
            )
        )
        .scalars()
        .all()
    }
    # Preserve the order the user selected them.
    ordered = [pieces_by_id[i] for i in ids if i in pieces_by_id]
    if not ordered:
        raise HTTPException(status_code=404, detail="none of the selected pieces are yours")

    pages = _allocate_sheet(ordered, layout=layout, start_row=start_row, start_col=start_col)
    rendered = request.app.state.templates.get_template("avery_sheet.html").render(
        pages=pages, layout=layout
    )
    if doc_type == "html":
        return HTMLResponse(rendered)
    body = pdf.render(rendered, options=pdf.LABEL_OPTIONS)
    # PDF rendered successfully → mark stock pieces as printed. Skip
    # already-printed/mailed/delivered pieces (they keep their state).
    now = utcnow()
    for p in ordered:
        if p.status == STATUS_GENERATED and p.archived_at is None:
            p.status = STATUS_PRINTED
            p.printed_at = p.printed_at or now
    await db.commit()
    filename = f"sheet_{len(ordered)}_pieces.pdf"
    return Response(
        content=body,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# Avery US Letter shipping/address label sheets. All three families share the
# same horizontal geometry (2 cols, 4" wide labels, 5/32" side margin, 3/16"
# gutter — verify: 5/32 + 4 + 3/16 + 4 + 5/32 = 8.5"). They differ only in
# label height + row count + top margin.
#
# `aliases` lists Avery model numbers Avery itself documents as identical
# templates (laser/inkjet/permanent variants). Users picking 8163 should land
# on the 5163 entry, etc. Not exhaustive — we only list the popular ones.
#
# `css_class` keys variant-specific font sizing in the sheet template, so a
# 1"-tall 5161 label gets smaller fonts than a 2"-tall 5163.
AVERY_LAYOUTS: dict[str, dict[str, Any]] = {
    "5163": {
        "name": "Avery 5163",
        "model": "5163",
        "aliases": ["8163", "5263", "5523", "5963", "8463"],
        "rows": 5,
        "cols": 2,
        "labels_per_sheet": 10,
        "label_width_in": 4.0,
        "label_height_in": 2.0,
        "top_margin_in": 0.5,
        "left_margin_in": 0.15625,  # 5/32"
        "col_pitch_in": 4.1875,  # label width + 3/16" gutter
        "row_pitch_in": 2.0,  # no vertical gutter
        "css_class": "label-5163",
    },
    "5162": {
        "name": "Avery 5162",
        "model": "5162",
        "aliases": ["8162", "5262", "8462"],
        "rows": 7,
        "cols": 2,
        "labels_per_sheet": 14,
        "label_width_in": 4.0,
        "label_height_in": 4.0 / 3.0,  # 1 1/3"
        "top_margin_in": 5.0 / 6.0,  # 0.8333" — (11 - 7*4/3) / 2
        "left_margin_in": 0.15625,
        "col_pitch_in": 4.1875,
        "row_pitch_in": 4.0 / 3.0,
        "css_class": "label-5162",
    },
    "5161": {
        "name": "Avery 5161",
        "model": "5161",
        "aliases": ["8161", "5261", "8461"],
        "rows": 10,
        "cols": 2,
        "labels_per_sheet": 20,
        "label_width_in": 4.0,
        "label_height_in": 1.0,
        "top_margin_in": 0.5,
        "left_margin_in": 0.15625,
        "col_pitch_in": 4.1875,
        "row_pitch_in": 1.0,
        "css_class": "label-5161",
    },
}

# Map every alias (and the primary model) to its primary key, so users can
# enter "8163" or "5163" interchangeably.
_LAYOUT_ALIAS_INDEX: dict[str, str] = {
    alias: key for key, layout in AVERY_LAYOUTS.items() for alias in [key, *layout["aliases"]]
}


def resolve_layout(model_or_alias: str) -> dict[str, Any]:
    """Resolve an Avery model number (or alias) to its layout dict.

    Raises HTTPException(400) on unknown input — caller is a request handler.
    """
    key = _LAYOUT_ALIAS_INDEX.get(model_or_alias.strip())
    if not key:
        raise HTTPException(
            status_code=400,
            detail=f"unknown Avery model {model_or_alias!r}; supported: "
            + ", ".join(sorted(_LAYOUT_ALIAS_INDEX)),
        )
    return AVERY_LAYOUTS[key]


# Backward-compat alias: tests + earlier callers reference this name.
AVERY_8163_LAYOUT: dict[str, Any] = AVERY_LAYOUTS["5163"]


def _allocate_sheet(
    pieces: list[MailPiece],
    *,
    layout: dict[str, Any],
    start_row: int,
    start_col: int,
) -> list[list[dict[str, Any]]]:
    """Lay out pieces row-by-row starting at (start_row, start_col) on
    page 1, then top-left of every subsequent page."""
    rows = layout["rows"]
    cols = layout["cols"]
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    r, c = start_row, start_col
    for piece in pieces:
        top = layout["top_margin_in"] + (r - 1) * layout["row_pitch_in"]
        left = layout["left_margin_in"] + (c - 1) * layout["col_pitch_in"]
        current.append(
            {
                "piece": piece,
                "top_in": top,
                "left_in": left,
            }
        )
        c += 1
        if c > cols:
            c = 1
            r += 1
        if r > rows:
            pages.append(current)
            current = []
            r, c = 1, 1
    if current:
        pages.append(current)
    return pages


@router.get("/{piece_id}/download/avery.{ext}")
async def download_avery(
    request: Request,
    piece_id: int,
    ext: str,
    db: SessionDep,
    user: CurrentUserDep,
    row: int = 1,
    col: int = 1,
    layout: str = "5163",
) -> Response:
    piece = await _load_owned(db, user.id, piece_id)
    layout_dict = resolve_layout(layout)
    if not (1 <= row <= layout_dict["rows"] and 1 <= col <= layout_dict["cols"]):
        raise HTTPException(status_code=400, detail="row/col out of range for this layout")
    return _render_piece_doc(request, piece, "avery", ext, row=row, col=col, layout=layout_dict)


def _render_piece_doc(
    request: Request,
    piece: MailPiece,
    format_type: str,
    ext: str,
    *,
    row: int = 1,
    col: int = 1,
    layout: dict[str, Any] | None = None,
) -> Response:
    if format_type == "envelope":
        template_name = "envelope.html"
        pdf_options = pdf.ENVELOPE_OPTIONS
    elif format_type == "avery":
        template_name = "avery.html"
        pdf_options = pdf.LABEL_OPTIONS
    else:  # pragma: no cover - URL constraints prevent this
        raise HTTPException(status_code=400, detail="unknown format")

    rendered = request.app.state.templates.get_template(template_name).render(
        sender_address=piece.sender_block,
        recipient_address=piece.recipient_block,
        human_readable_bar=piece.human_readable_imb(),
        barcode=piece.imb_letters,
        row=row,
        col=col,
        layout=layout or AVERY_LAYOUTS["5163"],
    )
    if ext == "html":
        return HTMLResponse(rendered)
    if ext == "pdf":
        body = pdf.render(rendered, options=pdf_options)
        filename = f"{format_type}_{piece.serial:06d}_{piece.recipient_zip_raw or 'na'}.pdf"
        return Response(
            content=body,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    raise HTTPException(status_code=400, detail="unknown extension")
