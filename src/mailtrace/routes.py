"""HTTP routes."""

from __future__ import annotations

import ipaddress
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import imb as imb_lib
from . import pdf
from .config import Settings
from .store import Store
from .usps import USPSClient, USPSError

log = logging.getLogger(__name__)

router = APIRouter()


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_store_dep(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


def get_usps_dep(request: Request) -> USPSClient:
    return request.app.state.usps  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
StoreDep = Annotated[Store, Depends(get_store_dep)]
USPSDep = Annotated[USPSClient, Depends(get_usps_dep)]


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.TemplateResponse(request, "index.html", {})
    return response


@router.get("/healthz")
async def healthz(store: StoreDep) -> dict[str, str]:
    try:
        await store.ping()
    except Exception as err:  # pragma: no cover - infrastructure failure
        raise HTTPException(status_code=503, detail=f"redis unavailable: {err}") from err
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Envelope generation
# ---------------------------------------------------------------------------


def _normalize_zip(value: str) -> tuple[str, str]:
    """Return (zip_full_for_human_readable, zip_routing_for_imb)."""
    zip_digits = "".join(ch for ch in value if ch.isdigit())
    if len(zip_digits) not in (5, 9, 11):
        raise HTTPException(status_code=400, detail="zip must be 5, 9 or 11 digits")
    if len(zip_digits) == 5:
        formatted = zip_digits
    else:
        formatted = zip_digits[:5] + "-" + zip_digits[5:9]
        if len(zip_digits) == 11:
            formatted += "-" + zip_digits[9:]
    return formatted, zip_digits


@router.post("/generate", response_class=HTMLResponse)
async def generate(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    sender_address: Annotated[str, Form()] = "",
    recipient_name: Annotated[str, Form()] = "",
    recipient_company: Annotated[str, Form()] = "",
    recipient_street: Annotated[str, Form()] = "",
    recipient_address2: Annotated[str, Form()] = "",
    recipient_city: Annotated[str, Form()] = "",
    recipient_state: Annotated[str, Form()] = "",
    recipient_zip: Annotated[str, Form()] = "",
) -> HTMLResponse:
    formatted_zip, raw_zip = _normalize_zip(recipient_zip)
    parts = [
        recipient_name.strip(),
        recipient_company.strip(),
        recipient_street.strip(),
        recipient_address2.strip(),
        f"{recipient_city.strip()}, {recipient_state.strip()}, {formatted_zip}",
    ]
    recipient_block = "\n".join(p for p in parts if p)
    serial = await store.next_serial()

    request.session["sender_address"] = sender_address
    request.session["recipient_address"] = recipient_block
    request.session["serial"] = serial
    request.session["recipient_zip"] = raw_zip

    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "generate.html",
        {"serial": serial, "recipient_zip": raw_zip},
    )
    return response


@router.get("/download/{format_type}/{doc_type}")
async def download(
    request: Request,
    format_type: str,
    doc_type: str,
    settings: SettingsDep,
    row: int = 1,
    col: int = 1,
) -> Response:
    sender_address = request.session.get("sender_address")
    recipient_address = request.session.get("recipient_address")
    serial = request.session.get("serial")
    recipient_zip = request.session.get("recipient_zip")
    if not (recipient_address and serial is not None and recipient_zip):
        raise HTTPException(status_code=400, detail="no envelope in session - generate first")

    barcode = imb_lib.encode(
        settings.barcode_id,
        settings.service_type_id,
        settings.mailer_id,
        int(serial),
        recipient_zip,
    )
    human_readable_bar = imb_lib.human_readable(
        settings.barcode_id,
        settings.service_type_id,
        settings.mailer_id,
        int(serial),
        recipient_zip,
    )

    if format_type == "envelope":
        template_name = "envelope.html"
        pdf_options = pdf.ENVELOPE_OPTIONS
    elif format_type == "avery":
        template_name = "avery.html"
        pdf_options = pdf.LABEL_OPTIONS
    else:
        raise HTTPException(status_code=400, detail="unknown format")

    rendered = request.app.state.templates.get_template(template_name).render(
        sender_address=sender_address,
        recipient_address=recipient_address,
        human_readable_bar=human_readable_bar,
        barcode=barcode,
        row=row,
        col=col,
    )

    if doc_type == "html":
        return HTMLResponse(rendered)
    if doc_type == "pdf":
        body = pdf.render(rendered, options=pdf_options)
        filename = f"{format_type}_{int(serial):06d}_{recipient_zip}.pdf"
        return Response(
            content=body,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    raise HTTPException(status_code=400, detail="unknown doc type")


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


class AddressIn(BaseModel):
    firmname: str = ""
    street_address: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""


@router.post("/validate_address")
async def validate_address(
    payload: Annotated[AddressIn, Form()],
    usps: USPSDep,
) -> JSONResponse:
    zip_digits = "".join(ch for ch in payload.zip if ch.isdigit())
    address: dict[str, str] = {
        "firmname": payload.firmname,
        "street_address": payload.street_address,
        "address2": payload.address2,
        "city": payload.city,
        "state": payload.state,
        "zip5": zip_digits[:5],
    }
    if len(zip_digits) >= 9:
        address["zip4"] = zip_digits[5:9]
    try:
        result = await usps.standardize_address(address)
    except USPSError as err:
        return JSONResponse({"error": str(err)}, status_code=502)
    return JSONResponse(result.to_dict())


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


class TrackQuery(BaseModel):
    serial: int = Field(..., ge=0)
    receipt_zip: str


@router.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "tracking.html", {}
    )
    return response


@router.get("/api/track")
async def track(
    serial: int,
    receipt_zip: str,
    settings: SettingsDep,
    usps: USPSDep,
    store: StoreDep,
) -> dict[str, Any]:
    zip_digits = "".join(ch for ch in receipt_zip if ch.isdigit())
    if len(zip_digits) not in (5, 9, 11):
        raise HTTPException(status_code=400, detail="zip must be 5, 9 or 11 digits")
    raw_imb = imb_lib.to_raw_imb(
        settings.barcode_id, settings.service_type_id, settings.mailer_id, serial, zip_digits
    )

    api_payload: dict[str, Any] = {}
    api_error: str | None = None
    try:
        api_payload = await usps.get_piece_tracking(raw_imb)
    except USPSError as err:
        api_error = str(err)
        log.warning("piece tracking failed: %s", err)

    stored_events = await store.get_events(raw_imb)
    data = api_payload.get("data") if isinstance(api_payload, dict) else None
    if not isinstance(data, dict):
        data = {"imb": raw_imb, "scans": []}
    scans = data.setdefault("scans", [])
    if not isinstance(scans, list):
        scans = []
        data["scans"] = scans
    # Stored push events are most authoritative + most recent; merge in order.
    for event in stored_events:
        scans.insert(0, event)
    return {"data": data, "error": api_error}


# ---------------------------------------------------------------------------
# IV push feed
# ---------------------------------------------------------------------------


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


def _resolve_caller(request: Request, trusted_proxies: list[str]) -> ipaddress._BaseAddress | None:
    """Return the originating client IP, only honoring X-Forwarded-For when
    the immediate peer is itself a trusted proxy. Returns None if the peer
    address is missing or unparseable.
    """
    if request.client is None:
        return None
    try:
        peer = ipaddress.ip_address(request.client.host)
    except ValueError:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff and _ip_in(peer, trusted_proxies):
        # Walk the chain right-to-left, skipping any hops that are also
        # trusted proxies, until we find the real client.
        for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
            try:
                hop_ip = ipaddress.ip_address(hop)
            except ValueError:
                continue
            if not _ip_in(hop_ip, trusted_proxies):
                return hop_ip
    return peer


def _feed_caller_allowed(
    request: Request,
    *,
    allowed: list[str],
    trusted_proxies: list[str],
    feed_open: bool,
) -> bool:
    if feed_open:
        return True
    if not allowed:
        return False  # fail closed: empty allowlist + feed_open=False = deny all
    caller = _resolve_caller(request, trusted_proxies)
    if caller is None:
        return False
    return _ip_in(caller, allowed)


@router.post("/usps_feed")
async def usps_feed(
    request: Request,
    settings: SettingsDep,
    store: StoreDep,
) -> dict[str, Any]:
    if not _feed_caller_allowed(
        request,
        allowed=settings.trusted_feed_ips,
        trusted_proxies=settings.trusted_proxies,
        feed_open=settings.feed_open,
    ):
        raise HTTPException(status_code=403, detail="caller not in trusted_feed_ips")
    payload = await request.json()
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="payload missing events list")

    stored = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        if "imb" not in event or event.get("handlingEventType") != "L":
            continue
        reformed = {
            "scan_date_time": event.get("scanDatetime"),
            "scan_event_code": event.get("scanEventCode"),
            "handling_event_type": event.get("handlingEventType"),
            "mail_phase": event.get("mailPhase"),
            "machine_name": event.get("machineName"),
            "scanner_type": event.get("scannerType"),
            "scan_facility_name": event.get("scanFacilityName"),
            "scan_facility_locale_key": event.get("scanLocaleKey"),
            "scan_facility_city": event.get("scanFacilityCity"),
            "scan_facility_state": event.get("scanFacilityState"),
            "scan_facility_zip": event.get("scanFacilityZip"),
        }
        await store.append_event(event["imb"], reformed)
        stored += 1
    return {"stored": stored}
