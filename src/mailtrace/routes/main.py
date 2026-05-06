"""Top-level routes: dashboard and healthz."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..auth import CurrentUserDep
from ..config import Settings
from ..db import SessionDep
from ..models import MailPiece
from ..store import Store
from ..usps import USPSClient

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


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    db: SessionDep,
    user: CurrentUserDep,
) -> HTMLResponse:
    recent = list(
        (
            await db.execute(
                select(MailPiece)
                .where(MailPiece.user_id == user.id, MailPiece.archived_at.is_(None))
                .order_by(MailPiece.created_at.desc(), MailPiece.id.desc())
                .limit(8)
            )
        )
        .scalars()
        .all()
    )
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"user": user, "recent_pieces": recent},
    )
    return response


@router.get("/favicon.ico", include_in_schema=False)
async def favicon_redirect() -> RedirectResponse:
    """Browsers request /favicon.ico unconditionally on first visit. Redirect
    to the actual SVG so we don't 404 (or worse, get bounced through auth)."""
    return RedirectResponse("/static/favicon.svg", status_code=301)


@router.get("/healthz")
async def healthz(store: StoreDep, db: SessionDep) -> dict[str, str]:
    """Liveness probe. Verifies the two backends the app actually needs."""
    try:
        await store.ping()
    except Exception as err:  # pragma: no cover - infrastructure failure
        raise HTTPException(status_code=503, detail=f"redis unavailable: {err}") from err
    try:
        from sqlalchemy import text

        await db.execute(text("SELECT 1"))
    except Exception as err:  # pragma: no cover - infrastructure failure
        raise HTTPException(status_code=503, detail=f"database unavailable: {err}") from err
    return {"status": "ok"}
