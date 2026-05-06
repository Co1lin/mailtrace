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
