"""Tests for app-factory helpers (no HTTP / lifespan)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailtrace.app import _ensure_sqlite_dir

# ---------------------------------------------------------------------------
# _ensure_sqlite_dir — regression coverage for the docker-compose deploy bug
# where urlparse+lstrip("/") corrupted absolute SQLite URLs into relative
# paths, causing PermissionError under WORKDIR=/app.
# ---------------------------------------------------------------------------


def test_ensure_sqlite_dir_absolute_creates_parent(tmp_path: Path) -> None:
    """4-slash absolute SQLite URL: sqlite:///<absolute-path-with-leading-/>.

    Since `tmp_path` is itself absolute (e.g. /tmp/pytest-xxx), three
    slashes in the format string + the leading '/' on the path produce
    SQLAlchemy's 4-slash absolute form.
    """
    db = tmp_path / "sub" / "x.db"
    assert not db.parent.exists()
    _ensure_sqlite_dir(f"sqlite+aiosqlite:///{db}")
    assert db.parent.exists()


def test_ensure_sqlite_dir_relative_creates_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3-slash relative SQLite URL: sqlite:///<relative-path>.

    The parent should be created relative to the *current* working
    directory at the time of the call (which is what SQLAlchemy itself
    will resolve against when opening the DB).
    """
    monkeypatch.chdir(tmp_path)
    _ensure_sqlite_dir("sqlite+aiosqlite:///./data/x.db")
    assert (tmp_path / "data").exists()


def test_ensure_sqlite_dir_memory_is_noop() -> None:
    """In-memory DB has no path; the helper must not raise or touch disk."""
    _ensure_sqlite_dir("sqlite+aiosqlite:///:memory:")


def test_ensure_sqlite_dir_non_sqlite_is_noop() -> None:
    """Non-sqlite URL (Postgres, etc.) is silently skipped."""
    _ensure_sqlite_dir("postgresql+asyncpg://u:p@host:5432/db")


def test_ensure_sqlite_dir_idempotent_when_parent_exists(tmp_path: Path) -> None:
    """Calling twice on the same absolute URL doesn't error."""
    db = tmp_path / "x.db"
    _ensure_sqlite_dir(f"sqlite+aiosqlite:///{db}")
    _ensure_sqlite_dir(f"sqlite+aiosqlite:///{db}")
    assert db.parent.exists()


def test_default_database_url_is_absolute() -> None:
    """The shipped default must point at /data (the docker volume mount),
    not the WORKDIR-relative ./data — otherwise the SQLite DB lives on
    the container's overlay filesystem and is lost on every recreate."""
    from sqlalchemy.engine.url import make_url

    from mailtrace.config import Settings

    s = Settings()
    db = make_url(s.database_url).database
    assert db == "/data/mailtrace.db", (
        f"default database_url must resolve to /data/mailtrace.db, got {db!r}. "
        f"Check that config.py uses the 4-slash absolute form."
    )


# ---------------------------------------------------------------------------
# init_db_sync — regression coverage for the multi-worker schema race.
# Without these, two uvicorn workers running lifespan in parallel would
# both call create_all and one would crash with "table already exists".
# The fix: do schema init ONCE in the CLI parent before forking workers.
# ---------------------------------------------------------------------------


def test_init_db_sync_creates_tables_on_empty_db(tmp_path: Path) -> None:
    """Fresh DB → init_db_sync creates the full schema."""
    import sqlite3

    from mailtrace.app import init_db_sync

    db_file = tmp_path / "x.db"
    init_db_sync(f"sqlite+aiosqlite:///{db_file}")
    assert db_file.exists()
    # Reach in with stdlib sqlite3 to confirm schema is real, not just
    # a SQLAlchemy-side cache.
    with sqlite3.connect(db_file) as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    # A handful of tables we know exist; don't pin the full list since
    # it grows over time.
    assert {"users", "addresses", "mailpieces", "scans", "app_config"} <= names


def test_init_db_sync_idempotent(tmp_path: Path) -> None:
    """Calling init_db_sync twice on the same DB must NOT raise.

    This is the property that protects multi-worker startup: even if
    something weird (e.g. a CI script, an operator) calls it twice,
    the second call is a no-op rather than a 'table already exists'.
    """
    from mailtrace.app import init_db_sync

    db_file = tmp_path / "x.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    init_db_sync(url)
    init_db_sync(url)  # must not raise


def test_lifespan_does_not_run_ddl(tmp_path: Path) -> None:
    """The FastAPI lifespan must NOT touch DDL — that's the CLI's job
    now. Concretely: if we point an app at a database file that has
    NEVER had init_db_sync run on it, the lifespan should still start
    cleanly (it doesn't query any tables), but a subsequent ORM query
    for a missing table should fail.

    This pins the invariant: schema creation lives outside the
    lifespan. Anyone who re-adds `await init_db(engine)` to the
    lifespan will break this test loudly.
    """
    import asyncio

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import create_async_engine

    from mailtrace.app import create_app
    from mailtrace.config import Settings

    db_file = tmp_path / "fresh.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    s = Settings(database_url=url, session_secret="test-only")
    app = create_app(s)

    async def _exercise() -> None:
        # Spin up the lifespan manually — we don't want to require
        # asgi-lifespan as a test dep just for one assertion. Use the
        # raw FastAPI primitive instead.
        async with app.router.lifespan_context(app):
            # If lifespan ran DDL, the table exists. We assert the
            # opposite: against a never-init'd DB, querying a model
            # table errors with "no such table".
            engine = create_async_engine(url)
            try:
                async with engine.begin() as conn:
                    with pytest.raises(OperationalError, match="no such table"):
                        await conn.exec_driver_sql("SELECT 1 FROM users")
            finally:
                await engine.dispose()

    asyncio.run(_exercise())
