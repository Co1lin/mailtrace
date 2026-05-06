"""Login, logout, password change, and per-user setup (Account) page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import auth as auth_lib
from ..db import SessionDep
from ..mail import Mailer, MailerError, OutgoingMessage, load_smtp_config
from ..models import User, utcnow
from ..usps import USPSClient, USPSError

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "auth/login.html", {"next": next, "error": None}
    )
    return response


@router.post("/login")
async def login(
    request: Request,
    db: SessionDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    email = email.strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or not auth_lib.verify_password(password, user.password_hash)
    ):
        templates = request.app.state.templates
        html: Response = templates.TemplateResponse(
            request,
            "auth/login.html",
            {"next": next, "error": "Invalid credentials."},
            status_code=401,
        )
        return html
    user.last_login_at = utcnow()
    await db.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    target = next if next.startswith("/") else "/"
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_form(request: Request) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "auth/change_password.html",
        {"user": user, "error": None, "forced": bool(user and user.must_change_password)},
    )
    return response


def _account_context(user: User) -> dict[str, object]:
    """Status flags the template uses to drive the section badges."""
    return {
        "mid_set": user.mailer_id is not None,
        "usps_api_set": bool(user.usps_client_id and user.usps_client_secret),
        "bcg_set": bool(user.bcg_username and user.bcg_password),
    }


@router.get("/account", response_class=HTMLResponse)
async def account_form(request: Request) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    flash = request.session.pop("account_flash", None)
    ctx: dict[str, object] = {
        "user": user,
        "error": None,
        "saved": False,
        "flash": flash,
    }
    if user is not None:
        ctx.update(_account_context(user))
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "auth/account.html", ctx
    )
    return response


@router.post("/account")
async def account_save(
    request: Request,
    db: SessionDep,
    mailer_id: Annotated[str, Form()] = "",
    barcode_id: Annotated[int, Form()] = 0,
    service_type_id: Annotated[int, Form()] = 40,
    usps_client_id: Annotated[str, Form()] = "",
    usps_client_secret: Annotated[str, Form()] = "",
    leave_usps_secret_unchanged: Annotated[bool, Form()] = False,
    bcg_username: Annotated[str, Form()] = "",
    bcg_password: Annotated[str, Form()] = "",
    leave_bcg_password_unchanged: Annotated[bool, Form()] = False,
    notify_on_scans: Annotated[bool, Form()] = False,
    notify_email: Annotated[str, Form()] = "",
) -> Response:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)
    templates = request.app.state.templates

    def render_error(msg: str) -> Response:
        ctx: dict[str, object] = {
            "user": user,
            "error": msg,
            "saved": False,
            "flash": None,
        }
        ctx.update(_account_context(user))
        resp: Response = templates.TemplateResponse(
            request, "auth/account.html", ctx, status_code=400
        )
        return resp

    mid: int | None
    if mailer_id.strip():
        try:
            mid = int(mailer_id.strip())
        except ValueError:
            return render_error("Mailer ID must be numeric.")
    else:
        mid = None
    if not (0 <= barcode_id <= 99):
        return render_error("Barcode ID must be 0-99.")
    if not (0 <= service_type_id <= 999):
        return render_error("Service Type ID must be 0-999.")

    user_in_db = await db.get(User, user.id)
    assert user_in_db is not None
    user_in_db.mailer_id = mid
    user_in_db.barcode_id = barcode_id
    user_in_db.service_type_id = service_type_id
    user_in_db.usps_client_id = usps_client_id.strip()
    if not leave_usps_secret_unchanged:
        user_in_db.usps_client_secret = usps_client_secret
        # Saving a new secret invalidates the cached probe result.
        user_in_db.usps_api_last_check = ""
    user_in_db.bcg_username = bcg_username.strip()
    if not leave_bcg_password_unchanged:
        user_in_db.bcg_password = bcg_password
        user_in_db.bcg_last_check = ""
    user_in_db.notify_on_scans = bool(notify_on_scans)
    user_in_db.notify_email = notify_email.strip() or None
    await db.commit()

    request.session["account_flash"] = "Saved."
    return RedirectResponse("/auth/account", status_code=303)


# ---------------------------------------------------------------------------
# Per-user credential test buttons. Each one talks to USPS using ONLY the
# logged-in user's saved credentials, then writes a one-line probe result
# back onto the User row so the next /auth/account render shows the badge.
# ---------------------------------------------------------------------------


def _summary_for(err: Exception) -> str:
    s = str(err)
    return (s[:480] + "…") if len(s) > 480 else s


@router.post("/account/test-usps-api")
async def test_usps_api(request: Request, db: SessionDep) -> Response:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)
    user_in_db = await db.get(User, user.id)
    assert user_in_db is not None
    usps: USPSClient = request.app.state.usps
    try:
        await usps.probe_modern_creds(user_in_db)
        user_in_db.usps_api_last_check = "ok"
        request.session["account_flash"] = "USPS API: token grant succeeded ✓"
    except USPSError as err:
        user_in_db.usps_api_last_check = f"fail: {_summary_for(err)}"
        request.session["account_flash"] = f"USPS API failed: {err}"
    await db.commit()
    return RedirectResponse("/auth/account", status_code=303)


@router.post("/account/test-bcg")
async def test_bcg(request: Request, db: SessionDep) -> Response:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)
    user_in_db = await db.get(User, user.id)
    assert user_in_db is not None
    usps: USPSClient = request.app.state.usps
    try:
        await usps.probe_legacy_creds(user_in_db)
        user_in_db.bcg_last_check = "ok"
        request.session["account_flash"] = "BCG: token grant succeeded ✓"
    except USPSError as err:
        user_in_db.bcg_last_check = f"fail: {_summary_for(err)}"
        request.session["account_flash"] = f"BCG failed: {err}"
    await db.commit()
    return RedirectResponse("/auth/account", status_code=303)


@router.post("/account/test-notify")
async def test_notify(request: Request, db: SessionDep) -> Response:
    """Send a test email to the user's notification address (or login email
    if no override) using the platform's SMTP config. Verifies the
    end-to-end notification path before scans actually start arriving."""
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)
    smtp = await load_smtp_config(db)
    if smtp is None or not smtp.enabled:
        request.session["account_flash"] = (
            "Notify test: the platform admin hasn't configured SMTP yet."
        )
        return RedirectResponse("/auth/account", status_code=303)
    target = (user.notify_email or user.email).strip()
    msg = OutgoingMessage(
        to=target,
        subject="mailtrace notification test",
        body_text=(
            f"This is a test notification for {user.email}.\n"
            "If you got this, scan-update emails will reach you at this address.\n"
        ),
        body_html=(
            f"<p>This is a test notification for <strong>{user.email}</strong>.</p>"
            "<p>If you got this, scan-update emails will reach you here.</p>"
        ),
    )
    mailer = Mailer(smtp)
    try:
        await mailer.send(msg)
        request.session["account_flash"] = f"Test email sent to {target} ✓"
    except MailerError as err:
        request.session["account_flash"] = f"Notify test failed: {err}"
    return RedirectResponse("/auth/account", status_code=303)


@router.post("/change-password")
async def change_password(
    request: Request,
    db: SessionDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/auth/login", status_code=303)

    templates = request.app.state.templates

    def render_error(msg: str) -> Response:
        resp: Response = templates.TemplateResponse(
            request,
            "auth/change_password.html",
            {"user": user, "error": msg, "forced": user.must_change_password},
            status_code=400,
        )
        return resp

    if not auth_lib.verify_password(current_password, user.password_hash):
        return render_error("Current password is incorrect.")
    if new_password != confirm_password:
        return render_error("New passwords do not match.")
    if len(new_password) < 10:
        return render_error("New password must be at least 10 characters.")
    if new_password == current_password:
        return render_error("New password must differ from the current one.")

    # Re-attach to this session so the update is committed correctly.
    user_in_db = await db.get(User, user.id)
    assert user_in_db is not None
    user_in_db.password_hash = auth_lib.hash_password(new_password)
    user_in_db.must_change_password = False
    await db.commit()
    return RedirectResponse("/", status_code=303)
