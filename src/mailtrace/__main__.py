"""Console entrypoint.

- ``python -m mailtrace serve`` (default if no args) → run the web server
- ``python -m mailtrace admin <command>`` → user administration

Also exposed as the ``mailtrace`` script via [project.scripts].
"""

from __future__ import annotations

import os
import sys

import uvicorn

from .app import _ensure_sqlite_dir, init_db_sync
from .cli import app as cli_app
from .config import get_settings


def _serve() -> None:
    host = os.getenv("MAILTRACE_HOST", "0.0.0.0")
    port = int(os.getenv("MAILTRACE_PORT", "8080"))
    workers = int(os.getenv("MAILTRACE_WORKERS", "1"))

    # Schema creation must happen ONCE in the parent process, BEFORE
    # uvicorn forks the worker pool. Otherwise every worker's lifespan
    # races `checkfirst` reflection against `CREATE TABLE` and emits a
    # noisy "table already exists" traceback on cold start. Doing it
    # here means the workers see a fully-initialised DB and their
    # lifespans only have to wire up engines / sessions / clients.
    settings = get_settings()
    _ensure_sqlite_dir(settings.database_url)
    init_db_sync(settings.database_url)

    # Do NOT let uvicorn rewrite request.client based on X-Forwarded-For
    # process-wide. /usps_feed authenticates with HTTP Basic Auth, so source
    # IP is logging-only — the ingest router walks XFF itself when the
    # immediate peer is in MAILTRACE_TRUSTED_PROXIES. Blanket rewriting
    # would just lie about the peer for every other endpoint.
    uvicorn.run(
        "mailtrace:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=False,
    )


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        sys.argv.pop(1)
        _serve()
        return
    if len(sys.argv) == 1:
        _serve()
        return
    cli_app()


if __name__ == "__main__":
    main()
