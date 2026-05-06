"""First-run setup. Available only while the users table is empty.

This is what replaces the CLI bootstrap step. The first visitor to a fresh
deployment can claim the admin account here. Once any user exists the
endpoints below 404, so a forgotten setup page is not a hijack vector.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import auth as auth_lib
from ..db import SessionDep
from ..models import User

router = APIRouter()


async def _users_exist(db) -> bool:  # type: ignore[no-untyped-def]
    found = (await db.execute(select(User.id).limit(1))).scalar_one_or_none()
    return found is not None


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, db: SessionDep) -> HTMLResponse:
    if await _users_exist(db):
        raise HTTPException(status_code=404, detail="setup already complete")
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "setup.html", {"error": None}
    )
    return response


@router.post("/setup")
async def setup_submit(
    request: Request,
    db: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    mailer_id: Annotated[str, Form()] = "",
) -> Response:
    if await _users_exist(db):
        raise HTTPException(status_code=409, detail="setup already complete")

    templates = request.app.state.templates

    def render_error(msg: str) -> Response:
        resp: Response = templates.TemplateResponse(
            request, "setup.html", {"error": msg}, status_code=400
        )
        return resp

    email = email.strip().lower()
    if "@" not in email or len(email) < 3:
        return render_error("Provide a valid email address.")
    if password != confirm_password:
        return render_error("Passwords do not match.")
    if len(password) < 10:
        return render_error("Password must be at least 10 characters.")

    mid: int | None = None
    if mailer_id.strip():
        try:
            mid = int(mailer_id.strip())
        except ValueError:
            return render_error("Mailer ID must be numeric.")

    user = User(
        email=email,
        password_hash=auth_lib.hash_password(password),
        is_admin=True,
        is_active=True,
        # The user just typed this password themselves, so don't force a
        # reset on first login.
        must_change_password=False,
        mailer_id=mid,
    )
    db.add(user)
    await db.commit()

    # Mark the cached has-user flag so the middleware short-circuits
    # immediately on the next request.
    request.app.state._has_user_cache = True

    return RedirectResponse("/auth/login", status_code=303)
