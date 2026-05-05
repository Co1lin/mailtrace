"""Console entrypoint: ``python -m mailtrace`` or ``mailtrace``.

Defers to uvicorn so users can also run ``uvicorn mailtrace:create_app --factory``
directly when they need full control.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("MAILTRACE_HOST", "0.0.0.0")
    port = int(os.getenv("MAILTRACE_PORT", "8080"))
    workers = int(os.getenv("MAILTRACE_WORKERS", "1"))
    uvicorn.run(
        "mailtrace:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
