"""Console entrypoint.

- ``python -m mailtrace serve`` (default if no args) → run the web server
- ``python -m mailtrace admin <command>`` → user administration

Also exposed as the ``mailtrace`` script via [project.scripts].
"""

from __future__ import annotations

import os
import sys

import uvicorn

from .cli import app as cli_app


def _serve() -> None:
    host = os.getenv("MAILTRACE_HOST", "0.0.0.0")
    port = int(os.getenv("MAILTRACE_PORT", "8080"))
    workers = int(os.getenv("MAILTRACE_WORKERS", "1"))
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
