"""Database engine, session factory, and helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _pool_pre_ping_for(url: str) -> bool:
    """Whether to enable SQLAlchemy's pool_pre_ping for this URL.

    pool_pre_ping issues a cheap SELECT-1 before handing out a pooled
    connection, to transparently recover from a NETWORK database server
    dropping idle connections. A local SQLite file has no socket to drop,
    so it buys nothing there — and worse, aiosqlite's async pool raises
    MissingGreenlet when the ping fires on a connection checked out from
    the background poll loop's long-idle path, which silently kills every
    poll cycle. So enable it only for non-SQLite (e.g. Postgres) backends.
    """
    try:
        backend = make_url(url).get_backend_name()
    except Exception:
        return False
    return backend != "sqlite"


def make_engine(url: str) -> AsyncEngine:
    # `future=True` is the default in SQLAlchemy 2.0; set echo via env if
    # you need it. SQLite needs a per-connection check_same_thread=False
    # which the aiosqlite driver handles automatically.
    return create_async_engine(url, pool_pre_ping=_pool_pre_ping_for(url))


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sm() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
