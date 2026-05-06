"""Authentication primitives: password hashing and the current-user accessor."""

from __future__ import annotations

import secrets
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request

from .models import User

_PEPPER = b""  # Set via MAILTRACE_PEPPER if you want extra defense; not exposed yet.


def hash_password(password: str) -> str:
    return bcrypt.hashpw((password.encode() + _PEPPER), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode() + _PEPPER, hashed.encode())
    except (ValueError, TypeError):
        return False


def generate_temp_password(nbytes: int = 9) -> str:
    """URL-safe random password used for invite + admin-reset flows."""
    return secrets.token_urlsafe(nbytes)


def current_user(request: Request) -> User:
    """Return the user attached by AuthMiddleware. Raises 401 if absent.

    Routes that should require auth take this as a dependency. The middleware
    has already redirected unauthenticated browsers; this dependency is the
    safety net for JSON / API callers and keeps mypy honest.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user  # type: ignore[no-any-return]


def require_admin(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return user


CurrentUserDep = Annotated[User, Depends(current_user)]
AdminUserDep = Annotated[User, Depends(require_admin)]
