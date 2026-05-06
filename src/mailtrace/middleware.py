"""Authentication middleware.

Resolves the session's `user_id` to a `User` object and stashes it on
`request.state.user` for downstream routes. Handles three flows:

1. Unauthenticated request to a private path → redirect to /auth/login.
2. Authenticated user with `must_change_password` → redirect to
   /auth/change-password (so freshly-invited users can't sit on a temp
   password indefinitely).
3. Non-admin hitting an /admin/ path → 403.

The check is applied as a Starlette pure middleware (not an app-level
exception handler) so it fires before any route resolution.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .models import User

# Paths that bypass auth entirely. Order: prefix match.
_PUBLIC_PREFIXES = (
    "/setup",  # first-run; gated by has-any-user check too
    "/auth/login",
    "/auth/logout",  # idempotent; clearing a missing session is a no-op
    "/static",
    "/healthz",
    "/usps_feed",  # has its own HTTP Basic Auth check
    "/docs",
    "/openapi.json",
    "/redoc",
)

# Paths reachable while the users table is empty (so the setup page
# itself can load static assets and redirect cleanly).
_PRE_SETUP_ALLOWED = ("/setup", "/static", "/healthz")

# Paths the user-must-change-password flow lets through.
_PW_RESET_BYPASS = (
    "/auth/change-password",
    "/auth/logout",
    "/static",
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)


def _is_pw_reset_allowed(path: str) -> bool:
    return any(path.startswith(p) for p in _PW_RESET_BYPASS)


async def _has_any_user(request: Request) -> bool:
    """Return True iff the users table has at least one row.

    Cached on app.state once True. Since users are never auto-deleted, the
    transition from empty → non-empty is one-way; once cached True we never
    re-query. The miss path is only walked during the brief bootstrap
    window before the first admin is created.
    """
    if getattr(request.app.state, "_has_user_cache", False):
        return True
    sm: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sm() as db:
        found = (await db.execute(select(User.id).limit(1))).scalar_one_or_none()
    if found is not None:
        request.app.state._has_user_cache = True
        return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        # First-run gate: until any user exists, only /setup (and a tiny
        # set of utility paths) are reachable; everything else redirects
        # to the setup form.
        if not await _has_any_user(request):
            if any(path == p or path.startswith(p + "/") for p in _PRE_SETUP_ALLOWED):
                return await call_next(request)
            return RedirectResponse("/setup", status_code=302)

        if _is_public(path):
            return await call_next(request)

        user_id = request.session.get("user_id") if "session" in request.scope else None
        if not user_id:
            return self._login_redirect(request)

        sm: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
        async with sm() as db:
            user = await db.get(User, int(user_id))
            if user is None or not user.is_active:
                request.session.clear()
                return self._login_redirect(request)

            if user.must_change_password and not _is_pw_reset_allowed(path):
                return RedirectResponse("/auth/change-password", status_code=302)

            if path.startswith("/admin") and not user.is_admin:
                return Response("forbidden", status_code=403)

            request.state.user = user
            return await call_next(request)

    @staticmethod
    def _login_redirect(request: Request) -> Response:
        # For HTML navigation, redirect; for JSON callers, return 401.
        accept = request.headers.get("accept", "")
        if "text/html" in accept or request.method == "GET":
            target = "/auth/login"
            if request.url.path != "/" and request.method == "GET":
                # Preserve where they wanted to go.
                from urllib.parse import quote

                target = f"/auth/login?next={quote(request.url.path)}"
            return RedirectResponse(target, status_code=302)
        return Response("authentication required", status_code=401)
