"""FastAPI application factory."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store.from_url(
            settings.redis_url,
            rolling_window_days=settings.serial_rolling_window,
            event_ttl_seconds=settings.feed_event_ttl_seconds,
        )
        usps = USPSClient(
            store=store,
            client_id=settings.usps_client_id.get_secret_value(),
            client_secret=settings.usps_client_secret.get_secret_value(),
            bcg_username=settings.bcg_username.get_secret_value(),
            bcg_password=settings.bcg_password.get_secret_value(),
        )
        app.state.settings = settings
        app.state.store = store
        app.state.usps = usps
        try:
            yield
        finally:
            await usps.aclose()
            await store.close()

    app = FastAPI(
        title="mailtrace",
        version="0.1.0",
        description="USPS first-class mail envelope generator with IMb tracking.",
        root_path=settings.root_path,
        lifespan=lifespan,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        same_site="lax",
        https_only=False,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["imb_font_data_uri"] = imb_font_data_uri()
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routes import router  # local import keeps route module self-contained

    app.include_router(router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return JSONResponse({"error": "internal server error"}, status_code=500)

    return app
