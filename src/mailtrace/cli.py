"""mailtrace admin CLI.

Usage:

    mailtrace admin create-user EMAIL [--admin] [--mailer-id N] [--password PW]
    mailtrace admin list-users
    mailtrace admin reset-password EMAIL [--password PW]
    mailtrace admin set-admin EMAIL [--off]
    mailtrace admin set-active EMAIL [--off]

Run inside the same environment as the app so it picks up the same
MAILTRACE_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import auth as auth_lib
from .app import init_db
from .config import get_settings
from .db import make_engine, make_sessionmaker
from .models import User

admin_app = typer.Typer(no_args_is_help=True, help="User administration commands.")
app = typer.Typer(no_args_is_help=True, help="mailtrace administration CLI.")
app.add_typer(admin_app, name="admin")


async def _with_session(
    coro: Callable[[AsyncSession], Awaitable[None]],
) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    await init_db(engine)
    sm = make_sessionmaker(engine)
    try:
        async with sm() as session:
            await coro(session)
    finally:
        await engine.dispose()


@admin_app.command("create-user")
def create_user(
    email: str,
    admin: Annotated[bool, typer.Option("--admin/--no-admin")] = False,
    mailer_id: Annotated[int | None, typer.Option("--mailer-id")] = None,
    password: Annotated[str | None, typer.Option("--password")] = None,
) -> None:
    """Create a new user. Prints a temp password if --password is omitted."""

    async def _run(db: AsyncSession) -> None:
        existing = (
            await db.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if existing is not None:
            typer.echo(f"error: user {email!r} already exists", err=True)
            raise typer.Exit(code=1)
        pw = password or auth_lib.generate_temp_password()
        user = User(
            email=email.lower(),
            password_hash=auth_lib.hash_password(pw),
            is_admin=admin,
            is_active=True,
            must_change_password=True,
            mailer_id=mailer_id,
        )
        db.add(user)
        await db.commit()
        typer.echo(f"created {user.email} (admin={admin}, mailer_id={mailer_id or '—'})")
        if not password:
            typer.echo(f"temporary password: {pw}")
            typer.echo("(user must change it on first login)")

    asyncio.run(_with_session(_run))


@admin_app.command("list-users")
def list_users() -> None:
    async def _run(db: AsyncSession) -> None:
        users = (await db.execute(select(User).order_by(User.id))).scalars().all()
        if not users:
            typer.echo("(no users)")
            return
        for u in users:
            tags = []
            if u.is_admin:
                tags.append("admin")
            if not u.is_active:
                tags.append("inactive")
            if u.must_change_password:
                tags.append("pw-reset")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            typer.echo(f"#{u.id}  {u.email}  MID={u.mailer_id or '—'}{tag_str}")

    asyncio.run(_with_session(_run))


@admin_app.command("reset-password")
def reset_password(
    email: str,
    password: Annotated[str | None, typer.Option("--password")] = None,
) -> None:
    async def _run(db: AsyncSession) -> None:
        user = (
            await db.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if user is None:
            typer.echo(f"error: no user {email!r}", err=True)
            raise typer.Exit(code=1)
        pw = password or auth_lib.generate_temp_password()
        user.password_hash = auth_lib.hash_password(pw)
        user.must_change_password = True
        await db.commit()
        typer.echo(f"reset {email}")
        if not password:
            typer.echo(f"temporary password: {pw}")

    asyncio.run(_with_session(_run))


@admin_app.command("set-admin")
def set_admin(
    email: str,
    off: Annotated[bool, typer.Option("--off", help="Demote instead.")] = False,
) -> None:
    async def _run(db: AsyncSession) -> None:
        user = (
            await db.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if user is None:
            typer.echo(f"error: no user {email!r}", err=True)
            raise typer.Exit(code=1)
        user.is_admin = not off
        await db.commit()
        typer.echo(f"{email}: is_admin={user.is_admin}")

    asyncio.run(_with_session(_run))


@admin_app.command("set-active")
def set_active(
    email: str,
    off: Annotated[bool, typer.Option("--off", help="Deactivate instead.")] = False,
) -> None:
    async def _run(db: AsyncSession) -> None:
        user = (
            await db.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if user is None:
            typer.echo(f"error: no user {email!r}", err=True)
            raise typer.Exit(code=1)
        user.is_active = not off
        await db.commit()
        typer.echo(f"{email}: is_active={user.is_active}")

    asyncio.run(_with_session(_run))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
