# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

# ---- builder: resolve and install dependencies into a venv ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src

# Install runtime deps (no dev) into /app/.venv from the locked manifest.
RUN uv venv /app/.venv && uv sync --no-dev --frozen

# ---- runtime: minimal image with reportlab fonts + a non-root user ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    MAILTRACE_HOST=0.0.0.0 \
    MAILTRACE_PORT=8080

# PDF generation is now pure Python (reportlab + pylabels) — no
# wkhtmltopdf/Qt dependency. Only system fonts are needed: DejaVu Serif
# for the recipient block. The IMb font ships in the package itself.
# curl is kept for the HEALTHCHECK below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 mailtrace
WORKDIR /app

COPY --from=builder --chown=mailtrace:mailtrace /app /app

# Persistent state (SQLite DB) lives here. Mount this in compose.
RUN mkdir -p /data && chown mailtrace:mailtrace /data
ENV MAILTRACE_DATABASE_URL=sqlite+aiosqlite:////data/mailtrace.db
VOLUME ["/data"]

USER mailtrace
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MAILTRACE_PORT}/healthz" || exit 1

CMD ["python", "-m", "mailtrace", "serve"]
