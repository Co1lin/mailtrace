"""Database engine, session factory, and helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str) -> AsyncEngine:
    # `future=True` is the default in SQLAlchemy 2.0; set echo via env if
    # you need it. SQLite needs a per-connection check_same_thread=False
    # which the aiosqlite driver handles automatically.
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with sm() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
