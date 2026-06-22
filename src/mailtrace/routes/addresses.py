"""Per-user address book (sender + recipient) CRUD."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUserDep
from ..db import SessionDep
from ..lob import LobClient, LobError
from ..models import Address

router = APIRouter()


_VALID_ROLES = {"sender", "recipient", "both"}

# Human-readable hint for Lob's non-"deliverable" verdicts, shown next to the
# Validate button. "deliverable" (and anything we don't have a message for)
# yields no warning.
_DELIVERABILITY_WARNINGS = {
    "deliverable_incorrect_unit": (
        "Building is valid but the apartment/unit looks wrong — double-check it."
    ),
    "deliverable_missing_unit": "Needs an apartment/unit number.",
    "deliverable_unnecessary_unit": "The apartment/unit isn't needed here.",
    "undeliverable": "USPS could not confirm this address is deliverable.",
}


def _deliverability_warning(deliverability: str) -> str | None:
    return _DELIVERABILITY_WARNINGS.get(deliverability)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def list_addresses(request: Request, db: SessionDep, user: CurrentUserDep) -> HTMLResponse:
    rows = (
        (
            await db.execute(
                select(Address).where(Address.user_id == user.id).order_by(Address.label)
            )
        )
        .scalars()
        .all()
    )
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "addresses/list.html", {"addresses": rows, "user": user}
    )
    return response


@router.get("/new", response_class=HTMLResponse)
async def new_address_form(request: Request, user: CurrentUserDep) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "addresses/edit.html",
        {"address": None, "user": user, "error": None},
    )
    return response


@router.post("/validate")
async def validate(
    request: Request,
    user: CurrentUserDep,
    firmname: Annotated[str, Form()] = "",
    street_address: Annotated[str, Form()] = "",
    address2: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    zip: Annotated[str, Form()] = "",
) -> JSONResponse:
    """Standardize an address against Lob (CASS-certified). Returns either
    {"address": {...}, "warning": "..."|null} on success or {"error": "..."}
    on failure. Used by the in-page Validate buttons on /pieces/new and
    /addresses/edit.

    Declared before the /{address_id} routes so the path matches before
    FastAPI tries to coerce "validate" into an int and 422s.
    """
    lob: LobClient = request.app.state.lob
    zip_digits = "".join(ch for ch in zip if ch.isdigit())
    payload: dict[str, str] = {
        "firmname": firmname,
        "street_address": street_address,
        "address2": address2,
        "city": city,
        "state": state,
        "zip5": zip_digits[:5],
    }
    if len(zip_digits) >= 9:
        payload["zip4"] = zip_digits[5:9]
    try:
        std = await lob.verify(user, payload)
    except LobError as err:
        return JSONResponse({"error": str(err)}, status_code=502)
    return JSONResponse(
        {"address": std.to_dict(), "warning": _deliverability_warning(std.deliverability)}
    )


@router.get("/{address_id}", response_class=HTMLResponse)
async def edit_address_form(
    request: Request, address_id: int, db: SessionDep, user: CurrentUserDep
) -> HTMLResponse:
    address = await _load_owned(db, user.id, address_id)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "addresses/edit.html",
        {"address": address, "user": user, "error": None},
    )
    return response


@router.post("")
@router.post("/")
async def create_address(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
    label: Annotated[str, Form()],
    role: Annotated[str, Form()] = "recipient",
    name: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    street: Annotated[str, Form()] = "",
    address2: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    zip: Annotated[str, Form()] = "",
) -> Response:
    if role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    label = label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    address = Address(
        user_id=user.id,
        label=label,
        role=role,
        name=name.strip(),
        company=company.strip(),
        street=street.strip(),
        address2=address2.strip(),
        city=city.strip(),
        state=state.strip().upper(),
        zip="".join(ch for ch in zip if ch.isdigit() or ch == "-"),
    )
    db.add(address)
    try:
        await db.commit()
    except Exception as err:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"label already in use: {err}") from err
    return RedirectResponse("/addresses/", status_code=303)


@router.post("/{address_id}")
async def update_address(
    address_id: int,
    db: SessionDep,
    user: CurrentUserDep,
    label: Annotated[str, Form()],
    role: Annotated[str, Form()] = "recipient",
    name: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    street: Annotated[str, Form()] = "",
    address2: Annotated[str, Form()] = "",
    city: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    zip: Annotated[str, Form()] = "",
) -> Response:
    if role not in _VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    address = await _load_owned(db, user.id, address_id)
    address.label = label.strip()
    address.role = role
    address.name = name.strip()
    address.company = company.strip()
    address.street = street.strip()
    address.address2 = address2.strip()
    address.city = city.strip()
    address.state = state.strip().upper()
    address.zip = "".join(ch for ch in zip if ch.isdigit() or ch == "-")
    await db.commit()
    return RedirectResponse("/addresses/", status_code=303)


@router.post("/{address_id}/delete")
async def delete_address(address_id: int, db: SessionDep, user: CurrentUserDep) -> Response:
    address = await _load_owned(db, user.id, address_id)
    await db.delete(address)
    await db.commit()
    return RedirectResponse("/addresses/", status_code=303)


async def _load_owned(db: AsyncSession, user_id: int, address_id: int) -> Address:
    address = await db.get(Address, address_id)
    if address is None or address.user_id != user_id:
        raise HTTPException(status_code=404, detail="address not found")
    return address
