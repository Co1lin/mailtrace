"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from . import mail, services
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .middleware import AuthMiddleware
from .models import AppConfig, Base
from .store import Store
from .usps import USPSClient

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


@lru_cache(maxsize=1)
def imb_font_data_uri() -> str:
    """Read the bundled IMb TTF and return a base64 data URI for @font-face."""
    font_bytes = (STATIC_DIR / "USPSIMBStandard.ttf").read_bytes()
    encoded = base64.b64encode(font_bytes).decode("ascii")
    return f"data:font/truetype;charset=utf-8;base64,{encoded}"


def _ensure_sqlite_dir(database_url: str) -> None:
    """For sqlite URLs, create the parent directory of the DB file.

    Uses SQLAlchemy's own URL parser instead of stdlib urlparse: the
    SQLAlchemy 4-slash absolute form (`sqlite+aiosqlite:////data/x.db`)
    means database=`/data/x.db`, but `urlparse(...).path.lstrip("/")`
    silently turns it into the relative `data/x.db` and writes into
    the WORKDIR — which is read-only for the non-root container user.
    """
    if not database_url.startswith("sqlite"):
        return
    db = make_url(database_url).database
    if not db or db == ":memory:":
        return
    parent = Path(db).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


# Columns added after the first release. `create_all` only creates tables
# that don't exist; it never adds new columns to existing ones. For each
# entry below, we ALTER TABLE … ADD COLUMN at startup if the column is
# missing — keeps existing SQLite/Postgres deployments upgrading cleanly
# without dragging in Alembic. Only safe for nullable columns or columns
# with a static default; never use this for NOT NULL without a default.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, full DDL fragment after ADD COLUMN)
    ("users", "timezone", "VARCHAR(64)"),
    # Drop-off record (when + where) for future map-overview features.
    # `shipped_from` defaults to "" so existing rows materialize cleanly
    # when the NOT-NULL column is added by SQLite (which ignores the
    # NOT NULL on ALTER unless a DEFAULT is given).
    ("mailpieces", "shipped_at", "TIMESTAMP"),
    ("mailpieces", "shipped_from", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("mailpieces", "shipped_from_lat", "FLOAT"),
    ("mailpieces", "shipped_from_lng", "FLOAT"),
)


def _apply_additive_columns_sync(sync_conn: Any) -> None:
    insp = inspect(sync_conn)
    for table, column, ddl in _ADDITIVE_COLUMNS:
        if not insp.has_table(table):
            continue  # create_all will have made it with the column already
        existing = {c["name"] for c in insp.get_columns(table)}
        if column in existing:
            continue
        sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        log.info("applied additive column: %s.%s %s", table, column, ddl)


async def init_db(engine: AsyncEngine) -> None:
    """Run create_all against an existing engine + apply additive ALTERs.

    Used by tests (which manage their own engine lifecycle) and by the
    `mailtrace admin` CLI commands (single-process, no race). The
    `mailtrace serve` entrypoint uses `init_db_sync` instead so that
    schema creation runs ONCE in the parent process before uvicorn
    forks worker pools — multi-worker startup would otherwise race
    `checkfirst` reflection against `CREATE TABLE` and produce noisy
    "table already exists" tracebacks on every cold start.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_additive_columns_sync)


def init_db_sync(database_url: str) -> None:
    """Run schema creation once, synchronously.

    Designed to be invoked by the CLI entrypoint *before* uvicorn spawns
    its worker pool. Spins up its own throwaway async engine, runs
    create_all, then disposes the engine. Safe to call multiple times
    on an already-initialised DB (create_all is idempotent within a
    single process).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _run() -> None:
        engine = create_async_engine(database_url)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()

    asyncio.run(_run())


DEFAULT_LOOP_INTERVAL_SECONDS = 300

# Redis key for the background-task leader lock. One of the running
# workers (uvicorn process / replica) holds this at any time; only that
# holder runs the poll/archive/dispatch cycle. Workers that lose / fail
# to acquire it just sleep, so HTTP-handling stays parallel while
# background work doesn't multiply.
_BG_LEADER_KEY = "mailtrace:bg_loop:leader"


async def _load_app_config(sm: async_sessionmaker[Any]) -> AppConfig:
    """Read (or lazily create) the AppConfig singleton.

    Multi-worker safe: if two workers race the first-time create, the
    one that loses the unique-constraint contest re-reads the row the
    winner inserted. From then on, both workers hit the SELECT path.
    """
    async with sm() as db:
        row: AppConfig | None = (
            await db.execute(select(AppConfig).where(AppConfig.id == 1))
        ).scalar_one_or_none()
        if row is None:
            row = AppConfig(id=1)
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                # Lost the race with another worker; re-read its insert.
                await db.rollback()
                row = (await db.execute(select(AppConfig).where(AppConfig.id == 1))).scalar_one()
            else:
                await db.refresh(row)
        # detach so the caller can read attrs after the session closes
        db.expunge(row)
        return row


async def _run_poll_loop(
    *,
    sm: async_sessionmaker[Any],
    store: Store,
    usps: USPSClient,
    worker_id: str,
    mailer_factory: Any = None,
) -> None:
    """Periodic background loop: pull tracking, auto-archive, send digests.

    Multi-worker safe via Redis leader election. Every uvicorn worker
    runs this loop, but at any moment only one worker holds the leader
    lock and actually does the work — the others read config, sleep,
    and stand by to take over if the leader dies.

    The lock TTL is 2x the configured interval (with a 60s safety
    margin), so a worker that crashes between renewals cedes the lock
    within one cycle. Failover is automatic: the next worker to wake up
    after the TTL expires just acquires the lock and proceeds.

    Reads cadence from the AppConfig singleton at the start of every
    iteration so changes made in /admin/settings take effect within one
    cycle. Per-piece USPS pulls use the *piece owner's* BCG credentials
    (loaded by services.poll_one); pieces whose owner has no BCG creds
    set will surface a per-piece error and back off, but won't stop the
    loop. Auto-archive and notification dispatch run on every cycle
    regardless of whether any piece is pollable.
    """
    log.info("background loop starting (worker=%s)", worker_id)
    held_leader = False
    try:
        while True:
            sleep_for = DEFAULT_LOOP_INTERVAL_SECONDS
            try:
                cfg = await _load_app_config(sm)
                sleep_for = max(30, int(cfg.poll_loop_interval_seconds))
                # TTL = 2x interval + slack. Long enough that one missed
                # renewal (network blip, GC pause, slow query) doesn't
                # cause spurious failover; short enough that a dead worker
                # cedes the lock within one cycle.
                lock_ttl = sleep_for * 2 + 60
                is_leader = await store.acquire_or_renew_leader(_BG_LEADER_KEY, worker_id, lock_ttl)
                if is_leader and not held_leader:
                    log.info("worker=%s became background-task leader", worker_id)
                elif not is_leader and held_leader:
                    log.info("worker=%s lost background-task leader", worker_id)
                held_leader = is_leader

                if not is_leader:
                    # Standby: just sleep and try again next cycle.
                    pass
                else:
                    async with sm() as db:
                        if cfg.poll_enabled:
                            due = await services.select_due_pieces(db, limit=cfg.poll_max_per_cycle)
                            for piece in due:
                                inserted, err = await services.poll_one(db, piece=piece, usps=usps)
                                if err:
                                    log.warning(
                                        "poll piece=%d errored: %s (errors=%d)",
                                        piece.id,
                                        err,
                                        piece.consecutive_poll_errors,
                                    )
                                elif inserted:
                                    log.info("poll piece=%d added %d scan(s)", piece.id, inserted)
                        if cfg.auto_archive_after_days > 0:
                            archived = await services.auto_archive_stale(
                                db, days=cfg.auto_archive_after_days
                            )
                            if archived:
                                log.info("auto-archived %d stale piece(s)", archived)
                        # Notification dispatch happens after scan ingestion
                        # in the same transaction so a failed send leaves
                        # last_notified_at un-bumped (and we retry next cycle).
                        smtp = await mail.load_smtp_config(db)
                        mailer = mailer_factory(smtp) if (mailer_factory and smtp) else None
                        sent = await services.dispatch_notifications(db, smtp=smtp, mailer=mailer)
                        if sent:
                            log.info("sent %d notification digest(s)", sent)
                        await db.commit()
            except asyncio.CancelledError:
                log.info("poll loop cancelled")
                raise
            except Exception:
                log.exception("poll loop iteration failed; retrying")

            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                log.info("poll loop cancelled during sleep")
                raise
    finally:
        # If we held the lock, release it so a peer worker can pick up
        # the work immediately instead of waiting for TTL expiry.
        if held_leader:
            with contextlib.suppress(Exception):
                await store.release_leader(_BG_LEADER_KEY, worker_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _ensure_sqlite_dir(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # NB: schema creation does NOT happen here. With multiple uvicorn
        # workers each running this lifespan independently, all workers
        # would race `checkfirst` reflection against CREATE TABLE and
        # one would lose with "table already exists". Schema init runs
        # once in the CLI parent process via init_db_sync() before fork.
        # Tests construct an engine and call init_db() directly.
        engine = make_engine(settings.database_url)
        sm = make_sessionmaker(engine)

        store = Store.from_url(
            settings.redis_url,
            rolling_window_days=settings.serial_rolling_window,
            event_ttl_seconds=settings.feed_event_ttl_seconds,
        )
        # USPSClient is per-process; methods take the calling user so
        # credentials and token caches are scoped per-tenant.
        usps = USPSClient(store=store)
        app.state.settings = settings
        app.state.store = store
        app.state.usps = usps
        app.state.db_engine = engine
        app.state.db_sessionmaker = sm

        # The poll loop runs in every worker but is gated by Redis-based
        # leader election, so only one worker actually does background
        # work at a time. Other workers stand by for failover.
        worker_id = uuid.uuid4().hex
        app.state.worker_id = worker_id
        poll_task: asyncio.Task[None] | None = asyncio.create_task(
            _run_poll_loop(
                sm=sm,
                store=store,
                usps=usps,
                worker_id=worker_id,
                mailer_factory=getattr(app.state, "mailer_factory", None),
            ),
            name="mailtrace-poll-loop",
        )

        try:
            yield
        finally:
            if poll_task is not None:
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await poll_task
            await usps.aclose()
            await store.close()
            await engine.dispose()

    app = FastAPI(
        title="MailTrace",
        version="0.1.0",
        description="USPS first-class mail envelope generator with IMb tracking.",
        root_path=settings.root_path,
        lifespan=lifespan,
    )

    # Auth middleware must run AFTER SessionMiddleware so it can read the
    # session. Starlette runs middlewares in reverse insertion order, so
    # AuthMiddleware is added first and SessionMiddleware second.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        same_site="lax",
        https_only=os.getenv("MAILTRACE_HTTPS_ONLY", "false").lower() == "true",
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["imb_font_data_uri"] = imb_font_data_uri()
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routes import router

    app.include_router(router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return JSONResponse({"error": "internal server error"}, status_code=500)

    return app
