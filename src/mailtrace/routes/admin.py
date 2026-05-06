"""Admin portal: user lifecycle (invite, activate, force reset, demote)."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import auth as auth_lib
from ..auth import AdminUserDep
from ..db import SessionDep
from ..mail import Mailer, MailerError, OutgoingMessage, load_smtp_config
from ..models import AppConfig, IngestLog, IngestSubscription, SmtpConfig, User

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_home(request: Request, db: SessionDep, _: AdminUserDep) -> HTMLResponse:
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    new_temp = request.session.pop("admin_flash_temp_password", None)
    new_temp_email = request.session.pop("admin_flash_temp_password_email", None)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "users": users,
            "user": _,
            "flash_temp_password": new_temp,
            "flash_temp_password_email": new_temp_email,
        },
    )
    return response


@router.post("/users")
async def create_user(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
    email: Annotated[str, Form()],
    is_admin: Annotated[bool, Form()] = False,
    mailer_id: Annotated[str, Form()] = "",
) -> Response:
    email = email.strip().lower()
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    temp = auth_lib.generate_temp_password()
    mid: int | None = None
    if mailer_id.strip():
        try:
            mid = int(mailer_id.strip())
        except ValueError as err:
            raise HTTPException(status_code=400, detail="mailer_id must be numeric") from err

    user = User(
        email=email,
        password_hash=auth_lib.hash_password(temp),
        is_admin=bool(is_admin),
        is_active=True,
        must_change_password=True,
        mailer_id=mid,
    )
    db.add(user)
    await db.commit()

    # Surface the temp password once via a session flash (admin-only context).
    request.session["admin_flash_temp_password"] = temp
    request.session["admin_flash_temp_password_email"] = email
    return RedirectResponse("/admin/", status_code=303)


@router.post("/users/{user_id}/force-reset")
async def force_reset(
    request: Request,
    user_id: int,
    db: SessionDep,
    admin: AdminUserDep,
    custom_password: Annotated[str, Form()] = "",
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if custom_password.strip():
        if len(custom_password) < 10:
            raise HTTPException(
                status_code=400, detail="custom password must be at least 10 characters"
            )
        new_password = custom_password
    else:
        new_password = auth_lib.generate_temp_password()
    target.password_hash = auth_lib.hash_password(new_password)
    target.must_change_password = True
    await db.commit()
    # Show the password (whether random or the admin's choice) once via a
    # flash so the admin can copy it out.
    request.session["admin_flash_temp_password"] = new_password
    request.session["admin_flash_temp_password_email"] = target.email
    return RedirectResponse("/admin/", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(
    request: Request, user_id: int, db: SessionDep, admin: AdminUserDep
) -> HTMLResponse:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "admin/edit_user.html",
        {"target": target, "user": admin, "error": None},
    )
    return response


@router.post("/users/{user_id}/edit")
async def edit_user_save(
    request: Request,
    user_id: int,
    db: SessionDep,
    admin: AdminUserDep,
    email: Annotated[str, Form()],
    mailer_id: Annotated[str, Form()] = "",
    barcode_id: Annotated[int, Form()] = 0,
    service_type_id: Annotated[int, Form()] = 40,
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")

    templates = request.app.state.templates

    def render_error(msg: str) -> Response:
        resp: Response = templates.TemplateResponse(
            request,
            "admin/edit_user.html",
            {"target": target, "user": admin, "error": msg},
            status_code=400,
        )
        return resp

    new_email = email.strip().lower()
    if new_email != target.email:
        clash = (await db.execute(select(User).where(User.email == new_email))).scalar_one_or_none()
        if clash is not None:
            return render_error("Another user already has that email.")
        target.email = new_email

    if mailer_id.strip():
        try:
            target.mailer_id = int(mailer_id.strip())
        except ValueError:
            return render_error("Mailer ID must be numeric.")
    else:
        target.mailer_id = None
    if not (0 <= barcode_id <= 99):
        return render_error("Barcode ID must be 0-99.")
    if not (0 <= service_type_id <= 999):
        return render_error("Service Type ID must be 0-999.")
    target.barcode_id = barcode_id
    target.service_type_id = service_type_id
    await db.commit()
    return RedirectResponse("/admin/", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: SessionDep,
    admin: AdminUserDep,
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    if target.is_admin:
        # Refuse to delete the last active admin (whether they're the
        # caller or someone else).
        active_admins = (
            (
                await db.execute(
                    select(User).where(User.is_admin.is_(True), User.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        if len(active_admins) <= 1:
            raise HTTPException(status_code=400, detail="cannot delete the last active admin")
    await db.delete(target)
    await db.commit()
    return RedirectResponse("/admin/", status_code=303)


@router.post("/users/{user_id}/toggle-active")
async def toggle_active(
    user_id: int,
    db: SessionDep,
    admin: AdminUserDep,
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == admin.id and target.is_active:
        raise HTTPException(status_code=400, detail="cannot deactivate yourself")
    target.is_active = not target.is_active
    await db.commit()
    return RedirectResponse("/admin/", status_code=303)


@router.post("/users/{user_id}/toggle-admin")
async def toggle_admin(
    user_id: int,
    db: SessionDep,
    admin: AdminUserDep,
) -> Response:
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == admin.id and target.is_admin:
        # Refuse to demote yourself if you're the last admin.
        admins = (
            (
                await db.execute(
                    select(User).where(User.is_admin.is_(True), User.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last active admin")
    target.is_admin = not target.is_admin
    await db.commit()
    return RedirectResponse("/admin/", status_code=303)


# ---------------------------------------------------------------------------
# SMTP config (singleton row at id=1)
# ---------------------------------------------------------------------------


_VALID_ENCRYPTIONS = {"starttls", "tls", "none"}


@router.get("/email", response_class=HTMLResponse)
async def email_form(request: Request, db: SessionDep, admin: AdminUserDep) -> HTMLResponse:
    cfg = await load_smtp_config(db)
    flash = request.session.pop("smtp_flash", None)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "admin/email.html",
        {"cfg": cfg, "user": admin, "error": None, "flash": flash},
    )
    return response


@router.post("/email")
async def email_save(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
    host: Annotated[str, Form()] = "",
    port: Annotated[int, Form()] = 587,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    encryption: Annotated[str, Form()] = "starttls",
    from_address: Annotated[str, Form()] = "",
    from_name: Annotated[str, Form()] = "",
    public_base_url: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
    leave_password_unchanged: Annotated[bool, Form()] = False,
) -> Response:
    if encryption not in _VALID_ENCRYPTIONS:
        raise HTTPException(status_code=400, detail="invalid encryption")
    cfg = await load_smtp_config(db)
    if cfg is None:
        cfg = SmtpConfig(id=1)
        db.add(cfg)
    cfg.host = host.strip()
    cfg.port = int(port)
    cfg.username = username.strip()
    if not leave_password_unchanged:
        cfg.password = password
    cfg.encryption = encryption
    cfg.from_address = from_address.strip()
    cfg.from_name = from_name.strip()
    cfg.public_base_url = public_base_url.strip()
    cfg.enabled = bool(enabled)
    await db.commit()
    request.session["smtp_flash"] = "Saved."
    return RedirectResponse("/admin/email", status_code=303)


# ---------------------------------------------------------------------------
# AppConfig (USPS API creds + poll cadence)
# ---------------------------------------------------------------------------


async def _load_app_config_row(db: SessionDep) -> AppConfig:
    """Read (or lazily create) the AppConfig singleton.

    Multi-worker safe: if another worker raced and inserted id=1 first,
    we catch the unique-constraint violation, roll back, and re-read.
    """
    from sqlalchemy.exc import IntegrityError

    cfg = (await db.execute(select(AppConfig).where(AppConfig.id == 1))).scalar_one_or_none()
    if cfg is None:
        cfg = AppConfig(id=1)
        db.add(cfg)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            cfg = (await db.execute(select(AppConfig).where(AppConfig.id == 1))).scalar_one()
        else:
            await db.refresh(cfg)
    return cfg


@router.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request, db: SessionDep, admin: AdminUserDep) -> HTMLResponse:
    cfg = await _load_app_config_row(db)
    flash = request.session.pop("settings_flash", None)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "admin/settings.html",
        {"cfg": cfg, "user": admin, "error": None, "flash": flash},
    )
    return response


@router.post("/settings")
async def settings_save(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
    poll_enabled: Annotated[bool, Form()] = False,
    poll_loop_interval_seconds: Annotated[int, Form()] = 300,
    poll_max_per_cycle: Annotated[int, Form()] = 50,
    auto_archive_after_days: Annotated[int, Form()] = 60,
) -> Response:
    if poll_loop_interval_seconds < 30 or poll_loop_interval_seconds > 86400:
        raise HTTPException(status_code=400, detail="poll interval must be 30-86400 seconds")
    if poll_max_per_cycle < 1 or poll_max_per_cycle > 1000:
        raise HTTPException(status_code=400, detail="poll_max_per_cycle must be 1-1000")
    if auto_archive_after_days < 0 or auto_archive_after_days > 3650:
        raise HTTPException(status_code=400, detail="auto_archive_after_days must be 0-3650")
    cfg = await _load_app_config_row(db)
    cfg.poll_enabled = bool(poll_enabled)
    cfg.poll_loop_interval_seconds = int(poll_loop_interval_seconds)
    cfg.poll_max_per_cycle = int(poll_max_per_cycle)
    cfg.auto_archive_after_days = int(auto_archive_after_days)
    await db.commit()
    request.session["settings_flash"] = "Saved. Changes take effect on the next poll cycle."
    return RedirectResponse("/admin/settings", status_code=303)


# ---------------------------------------------------------------------------
# USPS push-feed (IV-MTR) config
# ---------------------------------------------------------------------------


async def _load_ingest_cfg(db: SessionDep) -> IngestSubscription | None:
    return (
        await db.execute(select(IngestSubscription).where(IngestSubscription.id == 1))
    ).scalar_one_or_none()


@router.get("/ingest", response_class=HTMLResponse)
async def ingest_form(request: Request, db: SessionDep, admin: AdminUserDep) -> HTMLResponse:
    cfg = await _load_ingest_cfg(db)
    flash = request.session.pop("ingest_flash", None)
    flash_password = request.session.pop("ingest_flash_password", None)
    recent = list(
        (await db.execute(select(IngestLog).order_by(IngestLog.received_at.desc()).limit(20)))
        .scalars()
        .all()
    )
    settings = request.app.state.settings
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "admin/ingest.html",
        {
            "cfg": cfg,
            "user": admin,
            "error": None,
            "flash": flash,
            "flash_password": flash_password,
            "recent_logs": recent,
            "default_archive_dir": settings.ingest_archive_dir,
        },
    )
    return response


@router.post("/ingest")
async def ingest_save(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
    enabled: Annotated[bool, Form()] = False,
    basic_auth_user: Annotated[str, Form()] = "",
    basic_auth_pass: Annotated[str, Form()] = "",
    expect_gzip: Annotated[bool, Form()] = False,
    max_body_mb: Annotated[int, Form()] = 100,
    archive_payloads: Annotated[bool, Form()] = False,
    archive_dir: Annotated[str, Form()] = "",
    leave_password_unchanged: Annotated[bool, Form()] = False,
    rotate_password: Annotated[bool, Form()] = False,
) -> Response:
    if max_body_mb < 1 or max_body_mb > 1024:
        raise HTTPException(status_code=400, detail="max_body_mb must be 1-1024")
    cfg = await _load_ingest_cfg(db)
    if cfg is None:
        cfg = IngestSubscription(id=1)
        db.add(cfg)
    cfg.enabled = bool(enabled)
    cfg.basic_auth_user = basic_auth_user.strip()
    if rotate_password:
        new_pw = secrets.token_urlsafe(32)
        cfg.basic_auth_pass = new_pw
        request.session["ingest_flash_password"] = new_pw
    elif not leave_password_unchanged:
        cfg.basic_auth_pass = basic_auth_pass
    cfg.expect_gzip = bool(expect_gzip)
    cfg.max_body_mb = int(max_body_mb)
    cfg.archive_payloads = bool(archive_payloads)
    cfg.archive_dir = archive_dir.strip()
    if cfg.enabled and not cfg.basic_auth_user:
        raise HTTPException(status_code=400, detail="basic auth username is required when enabled")
    if cfg.enabled and not cfg.basic_auth_pass:
        raise HTTPException(
            status_code=400,
            detail="basic auth password is required when enabled (use 'rotate' to generate one)",
        )
    await db.commit()
    request.session["ingest_flash"] = "Saved."
    return RedirectResponse("/admin/ingest", status_code=303)


@router.post("/ingest/test")
async def ingest_self_test(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
) -> Response:
    """Synthetic POST to the local /usps_feed endpoint with a single
    fake event. Used to verify auth and parsing without waiting on USPS."""
    import base64

    import httpx

    cfg = await _load_ingest_cfg(db)
    if cfg is None or not cfg.enabled:
        request.session["ingest_flash"] = "Test: enable the receiver first."
        return RedirectResponse("/admin/ingest", status_code=303)
    if not (cfg.basic_auth_user and cfg.basic_auth_pass):
        request.session["ingest_flash"] = "Test: set Basic Auth credentials first."
        return RedirectResponse("/admin/ingest", status_code=303)

    payload = {
        "feedId": "self-test",
        "feedName": "mailtrace self-test",
        "fileGenerationDateTime": "2025-01-01T00:00:00Z",
        "recordCount": 1,
        "data": [
            {
                "imb": "selftest-no-such-imb",
                "scanDateTime": "2025-01-01T00:00:00Z",
                "scanEventCode": "SP",
                "scanFacilityCity": "TEST",
                "scanFacilityState": "CA",
                "scanFacilityZIP": "00000",
            }
        ],
    }
    creds = f"{cfg.basic_auth_user}:{cfg.basic_auth_pass}".encode()
    auth_header = "Basic " + base64.b64encode(creds).decode("ascii")

    transport = httpx.ASGITransport(app=request.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/usps_feed",
                content=__import__("json").dumps(payload),
                headers={"authorization": auth_header, "content-type": "application/json"},
            )
        if resp.status_code == 200:
            request.session["ingest_flash"] = (
                f"Self-test OK ({resp.status_code}): {resp.text[:200]}"
            )
        else:
            request.session["ingest_flash"] = (
                f"Self-test failed ({resp.status_code}): {resp.text[:200]}"
            )
    except Exception as err:
        request.session["ingest_flash"] = f"Self-test error: {err}"
    return RedirectResponse("/admin/ingest", status_code=303)


# ---------------------------------------------------------------------------
# SMTP test
# ---------------------------------------------------------------------------


@router.post("/email/test")
async def email_test(
    request: Request,
    db: SessionDep,
    admin: AdminUserDep,
    test_recipient: Annotated[str, Form()] = "",
) -> Response:
    cfg = await load_smtp_config(db)
    if cfg is None:
        raise HTTPException(status_code=400, detail="save SMTP config first")
    target = (test_recipient or admin.email).strip()
    mailer = Mailer(cfg)
    msg = OutgoingMessage(
        to=target,
        subject="mailtrace SMTP test",
        body_text=(
            "This is a test email from your mailtrace deployment.\n"
            "If you got this, the SMTP settings are working.\n"
        ),
        body_html=(
            "<p>This is a test email from your <strong>mailtrace</strong> deployment.</p>"
            "<p>If you got this, the SMTP settings are working.</p>"
        ),
    )
    try:
        await mailer.send(msg)
    except MailerError as err:
        request.session["smtp_flash"] = f"Test failed: {err}"
        return RedirectResponse("/admin/email", status_code=303)
    request.session["smtp_flash"] = f"Test email sent to {target}."
    return RedirectResponse("/admin/email", status_code=303)
