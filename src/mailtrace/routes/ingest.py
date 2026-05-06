"""USPS Informed Visibility (IV-MTR) HTTPS-JSON push receiver.

USPS schedules a Data Delivery subscription that POSTs JSON to a URL we
own. Their source IPs aren't published, so we authenticate by HTTP Basic
Auth (configured in the admin portal). Body can be large (single-digit MB
to ~100MB depending on volume) and may be gzip-compressed.

Spec highlights:
  - Respond with 200 quickly. USPS retries on 5xx → IngestLog dedup keys
    the per-scan upserts.
  - Wrapper key for the events array varies by IV portal config: try
    "data" / "records" / "events" / "scans".
  - Field names within each event vary too: services._normalize_event /
    services.imb_from_event do tolerant lookups.
  - Archive raw payloads to disk so the same delivery can be replayed
    after a parser fix without depending on USPS to re-send.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hmac
import ipaddress
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from .. import services
from ..config import Settings
from ..db import SessionDep
from ..models import IngestLog, IngestSubscription, MailPiece, utcnow

router = APIRouter()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_basic_auth(header: str, expected_user: str, expected_pass: str) -> bool:
    """Constant-time validation of an Authorization: Basic header."""
    if not expected_user or not expected_pass:
        return False
    if not header.lower().startswith("basic "):
        return False
    encoded = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    if ":" not in decoded:
        return False
    user, _, pw = decoded.partition(":")
    # compare both fields even on failure to keep timing flat-ish.
    user_ok = hmac.compare_digest(user.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(pw.encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


def _resolve_source_ip(request: Request, trusted_proxies: list[str]) -> str:
    """Best-effort source IP for logging. If the immediate peer is in
    trusted_proxies, walk the X-Forwarded-For chain. Auth doesn't depend
    on this — it's purely for the audit log."""
    if request.client is None:
        return ""
    peer = request.client.host
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not _ip_in(peer_ip, trusted_proxies):
        return peer
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        try:
            hop_ip = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if not _ip_in(hop_ip, trusted_proxies):
            return str(hop_ip)
    return peer


def _ip_in(candidate: ipaddress._BaseAddress, entries: list[str]) -> bool:
    for entry in entries:
        try:
            if "/" in entry:
                if candidate in ipaddress.ip_network(entry, strict=False):
                    return True
            elif candidate == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


_WRAPPER_KEYS = ("data", "records", "events", "scans", "Data", "Records", "Events", "Scans")


def _extract_events(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    """Return (feed_id, events). The IV file is wrapped in an object whose
    array key varies by deployment — try the common ones, fall back to
    treating a bare list as the events array."""
    if isinstance(payload, list):
        return "", [e for e in payload if isinstance(e, dict)]
    if not isinstance(payload, dict):
        return "", []
    feed_id = ""
    for key in ("feedId", "feed_id", "FeedID", "subscriptionId"):
        v = payload.get(key)
        if isinstance(v, str | int):
            feed_id = str(v)
            break
    for key in _WRAPPER_KEYS:
        v = payload.get(key)
        if isinstance(v, list):
            return feed_id, [e for e in v if isinstance(e, dict)]
    return feed_id, []


def _archive_raw(settings: Settings, cfg: IngestSubscription, raw: bytes) -> str:
    """Write the raw (decompressed) JSON to disk under YYYY/MM/DD/, return
    the absolute path. Errors are best-effort — archive failure must not
    break the delivery 200."""
    base = Path(cfg.archive_dir.strip() or settings.ingest_archive_dir)
    now = datetime.now()
    target = (
        base
        / f"{now:%Y}"
        / f"{now:%m}"
        / f"{now:%d}"
        / f"{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return str(target)


async def _write_log(
    db: SessionDep,
    *,
    request: Request,
    settings: Settings,
    feed_id: str = "",
    record_count: int = 0,
    new_scans: int = 0,
    matched: int = 0,
    orphaned: int = 0,
    bytes_received: int = 0,
    raw_path: str = "",
    status: str = "received",
    error: str = "",
) -> None:
    db.add(
        IngestLog(
            source_ip=_resolve_source_ip(request, settings.trusted_proxies)[:64],
            feed_id=feed_id[:64],
            record_count=record_count,
            new_scans=new_scans,
            matched=matched,
            orphaned=orphaned,
            bytes_received=bytes_received,
            raw_path=raw_path[:1024],
            status=status[:16],
            error=error[:2000],
        )
    )


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------


async def _gate_enabled_and_authed(request: Request, db: SessionDep) -> IngestSubscription:
    """Shared 503/401 gate for every /usps_feed entry point (POST + the
    GET/HEAD probes USPS uses for "Test Server Connection"). Returns
    the IngestSubscription row on success; raises HTTPException
    otherwise (and writes a `failed` IngestLog row on auth miss)."""
    settings: Settings = request.app.state.settings
    cfg = (
        await db.execute(select(IngestSubscription).where(IngestSubscription.id == 1))
    ).scalar_one_or_none()
    if cfg is None or not cfg.enabled:
        # Don't 404 — if the operator briefly toggles the feature off,
        # USPS retrying gives us a chance to flip it back on without
        # losing data. 503 is the polite "down for maintenance" code.
        raise HTTPException(status_code=503, detail="ingest disabled")

    auth_header = request.headers.get("authorization", "")
    if not _check_basic_auth(auth_header, cfg.basic_auth_user, cfg.basic_auth_pass):
        log.warning(
            "usps_feed auth failure from %s", request.client.host if request.client else "?"
        )
        await _write_log(
            db,
            request=request,
            settings=settings,
            status="failed",
            error="auth failure",
        )
        await db.commit()
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="usps-iv"'},
        )
    return cfg


def _probe_response() -> Response:
    """Body USPS' Test Server Connection (and any other probe) gets when
    everything is wired correctly."""
    return Response(
        content=json.dumps({"status": "ok", "probe": True}),
        media_type="application/json",
        status_code=200,
    )


@router.get("/usps_feed", include_in_schema=False)
@router.head("/usps_feed", include_in_schema=False)
async def usps_feed_probe(request: Request, db: SessionDep) -> Response:
    """USPS' Test Server Connection occasionally probes with GET / HEAD
    instead of an empty POST. Same auth+enabled gate as the POST handler;
    returns 200 if your credentials work and the receiver is enabled,
    so USPS' control panel sees a green check.

    The actual delivery method is always POST — this is purely a probe
    affordance for clients that don't trust an empty POST body.
    """
    cfg = await _gate_enabled_and_authed(request, db)
    settings: Settings = request.app.state.settings
    await _write_log(
        db,
        request=request,
        settings=settings,
        status="probe",
        error=f"probe via {request.method}",
    )
    await db.commit()
    _ = cfg  # quiet ruff; the call above is the gate
    return _probe_response()


@router.post("/usps_feed")
async def usps_feed(request: Request, db: SessionDep) -> Response:
    """Receive a USPS IV-MTR Data Delivery POST.

    Returns 200 on success (whether or not all events matched a piece);
    200 on an empty body (the "Test Server Connection" probe USPS sends
    with no payload); 401 on auth failure; 413 on body too large; 400 on
    malformed body; 503 when the subscription is disabled (so USPS knows
    to retry rather than treating us as gone).
    """
    settings: Settings = request.app.state.settings
    cfg = await _gate_enabled_and_authed(request, db)

    declared = request.headers.get("content-length")
    max_bytes = max(1, cfg.max_body_mb) * 1024 * 1024
    if declared:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(status_code=413, detail="body too large")
        except ValueError:
            pass  # bogus header — fall through to actual size check below

    raw_bytes = await request.body()
    bytes_received = len(raw_bytes)

    # USPS' "Test Server Connection" sends an empty (or whitespace-only)
    # POST body to verify reachability + credentials. Don't try to JSON-
    # parse zero bytes — that would 400 and USPS would mark the
    # connection as failed even though everything is wired correctly.
    # We've already validated auth in the gate above, so a 200 here
    # means "your creds work AND the receiver is enabled".
    if not raw_bytes.strip():
        await _write_log(
            db,
            request=request,
            settings=settings,
            status="probe",
            error="probe via POST (empty body)",
        )
        await db.commit()
        return _probe_response()

    if bytes_received > max_bytes:
        await _write_log(
            db,
            request=request,
            settings=settings,
            bytes_received=bytes_received,
            status="failed",
            error=f"body {bytes_received} > limit {max_bytes}",
        )
        await db.commit()
        raise HTTPException(status_code=413, detail="body too large")

    # Auto-detect gzip via the magic bytes (1f 8b) — gzip files always
    # start with them, so this is more reliable than trusting a USPS
    # Content-Encoding header (which they sometimes omit) or an operator
    # who set IngestSubscription.expect_gzip but USPS is actually sending
    # plain JSON (or vice-versa). Plain JSON cannot start with these
    # bytes, so false positives are impossible.
    enc = request.headers.get("content-encoding", "").lower()
    looks_gzipped = raw_bytes.startswith(b"\x1f\x8b")
    if looks_gzipped or "gzip" in enc:
        try:
            raw_bytes = gzip.decompress(raw_bytes)
        except (OSError, EOFError) as err:
            await _write_log(
                db,
                request=request,
                settings=settings,
                bytes_received=bytes_received,
                status="failed",
                error=f"gzip: {err}",
            )
            await db.commit()
            raise HTTPException(status_code=400, detail="invalid gzip body") from err

    archive_path = ""
    if cfg.archive_payloads:
        try:
            archive_path = _archive_raw(settings, cfg, raw_bytes)
        except OSError as err:
            log.warning("ingest archive failed: %s", err)
            # carry on — archive is best-effort

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        await _write_log(
            db,
            request=request,
            settings=settings,
            bytes_received=bytes_received,
            raw_path=archive_path,
            status="failed",
            error=f"json: {err}",
        )
        await db.commit()
        raise HTTPException(status_code=400, detail="invalid JSON body") from err

    feed_id, events = _extract_events(payload)
    new_scans = matched = orphaned = 0
    for raw_event in events:
        imb_value = services.imb_from_event(raw_event)
        if not imb_value:
            orphaned += 1
            continue
        piece = (
            await db.execute(select(MailPiece).where(MailPiece.imb_raw == imb_value))
        ).scalar_one_or_none()
        if piece is None:
            orphaned += 1
            continue
        matched += 1
        if await services.ingest_scan(db, piece, raw_event, source="feed"):
            new_scans += 1

    cfg.last_received_at = utcnow()
    await _write_log(
        db,
        request=request,
        settings=settings,
        feed_id=feed_id,
        record_count=len(events),
        new_scans=new_scans,
        matched=matched,
        orphaned=orphaned,
        bytes_received=bytes_received,
        raw_path=archive_path,
        status="parsed",
    )
    await db.commit()
    return Response(
        content=json.dumps(
            {
                "stored": new_scans,
                "matched": matched,
                "orphaned": orphaned,
                "records": len(events),
            }
        ),
        media_type="application/json",
        status_code=200,
    )
