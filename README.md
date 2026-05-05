# mailtrace

Generate USPS first-class mail envelopes with a printed Intelligent Mail
Barcode (IMb), then track each piece through USPS Informed Visibility — for
the price of a normal stamp, no Certified Mail required.

This is a re-engineered re-implementation of [1997cui/envelope][upstream]
with modern tooling, type hints, tests, a small Dockerfile, and env-driven
configuration. Releases are published as multi-arch container images to
GHCR (`ghcr.io/co1lin/mailtrace`) so consumers only need a compose file.

[upstream]: https://github.com/1997cui/envelope

## Features

- **Envelope generator** — #10 envelope (PDF/HTML) and Avery 8163 sticker
  layouts, with the bundled USPS IMb font embedded inline (no asset paths
  to wire up).
- **Address standardization** via the new USPS API at `apis.usps.com`.
- **Live piece tracking** via the IV `get/piece/imb/<imb>` endpoint plus an
  optional **push feed** (`POST /usps_feed`) so scan events streamed by IV
  show up immediately.
- **Stateless app, Redis-backed state** — serial allocation, IV access
  tokens, and pushed scan events all live in Redis with sensible TTLs.
- **First-class observability** — `/healthz` for the orchestrator,
  structured JSON errors, OpenAPI docs at `/docs`.

## Architecture

```
┌──────────────┐  POST events   ┌─────────┐
│  USPS IV     │ ─────────────▶ │         │
│  (push feed) │                │         │
└──────────────┘                │         │     ┌─────────┐
                                │ FastAPI │ ◀─▶ │  Redis  │
┌──────────────┐  GET tracking  │         │     └─────────┘
│  USPS APIs   │ ◀───────────── │         │
└──────────────┘                └─────────┘
                                     ▲
                                     │ HTML / PDF / JSON
                                     ▼
                                  browser
```

## Quick start (Docker)

Pull the published image — no clone required:

```bash
mkdir mailtrace && cd mailtrace
curl -fLO https://raw.githubusercontent.com/Co1lin/mailtrace/main/docker-compose.yml
curl -fL https://raw.githubusercontent.com/Co1lin/mailtrace/main/.env.example -o .env
$EDITOR .env             # fill in MAILER_ID, USPS_CLIENT_ID/SECRET, SESSION_SECRET
docker compose up -d
```

Or, if you cloned the repo and want to build locally instead:

```bash
MAILTRACE_IMAGE= docker compose up -d --build
```

The app listens on `127.0.0.1:8084` by default. Override the publish via
`MAILTRACE_BIND` (e.g. `MAILTRACE_BIND=0.0.0.0:8080`). All settings are
read from `MAILTRACE_*` env vars — see [`.env.example`](.env.example) for
the full list.

To put it behind a reverse proxy under a subpath (the way the upstream
`/envelope` deployment does), set `MAILTRACE_ROOT_PATH=/envelope`.

To pin which IPs may push to `/usps_feed`, set:

```env
MAILTRACE_TRUSTED_FEED_IPS=["56.0.0.0/8"]
```

## Local development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pre-commit install
uv run pytest                       # 25 tests, no network
uv run ruff check . && uv run mypy
uv run mailtrace                    # serves on http://127.0.0.1:8080
```

For PDF generation locally, install `wkhtmltopdf` (e.g. `apt install
wkhtmltopdf` or `brew install --cask wkhtmltopdf`). The Docker image
already bundles it.

## Endpoints

| Route | Method | Description |
| --- | --- | --- |
| `/` | GET | Address entry + tracking form |
| `/generate` | POST | Allocate serial, render envelope page |
| `/download/envelope/{html\|pdf}` | GET | Download #10 envelope |
| `/download/avery/{html\|pdf}?row=&col=` | GET | Download Avery 8163 cell |
| `/validate_address` | POST | Standardize an address (form-encoded) |
| `/tracking?serial=&receipt_zip=` | GET | Tracking results page |
| `/api/track?serial=&receipt_zip=` | GET | JSON tracking payload (live + pushed events) |
| `/usps_feed` | POST | Receiver for the IV push feed |
| `/healthz` | GET | Liveness probe (pings Redis) |
| `/docs` | GET | OpenAPI / Swagger UI |

## USPS credentials — what you need

1. A **Business Customer Gateway** account at
   <https://gateway.usps.com/>; request access to **Informed Visibility –
   Mail Tracking & Reporting (IV-MTR)**. The gateway issues your
   **Mailer ID** (MID).
2. A developer app at <https://developer.usps.com/> with the *Addresses*
   product enabled. The portal gives you a **Client ID / Client Secret**
   for `apis.usps.com` (used here for address standardization).
3. To use the IMb piece-tracking endpoint, your BCG username/password is
   exchanged for an OAuth token against `services.usps.com`.

The original write-up at
<https://blog.ctyi.me/%E7%94%9F%E6%B4%BB/2021/06/03/USPS_IV_MTR.html>
walks through enrolment in detail.

## How tracking works

USPS scanners read the IMb during sortation and emit a stream of events.
mailtrace gets those two ways:

1. **On demand** by hitting the IV piece endpoint when a user opens the
   tracking page. This always returns the most recent normalized state.
2. **Push** — IV can be configured to POST events as they happen. We
   keep them in Redis (60-day TTL) keyed by IMb so the tracking page can
   show them even before they appear in the on-demand summary, and so
   they survive past USPS's own retention window.

## Project layout

```
src/mailtrace/
├── app.py          # FastAPI factory + lifespan
├── config.py       # MAILTRACE_* env settings (pydantic-settings)
├── routes.py       # all HTTP routes
├── store.py        # Redis: serial allocator + event log
├── usps.py         # USPS API client (oauth + tracking + addresses)
├── imb.py          # Intelligent Mail Barcode encoder
├── pdf.py          # wkhtmltopdf options
├── static/USPSIMBStandard.ttf
└── templates/      # Jinja: index, generate, tracking, envelope, avery
tests/              # pytest, fakeredis, no network calls
Dockerfile          # multi-stage, slim, non-root, healthcheck
docker-compose.yml  # app + redis with named volume
```

## License & attribution

Licensed under the **GNU Affero General Public License v3** (see
[`LICENSE`](LICENSE)) to honor the upstream license. The IMb encoder is
adapted from the original Python implementation by Sam Rushing
(Simplified BSD); see [`NOTICE`](NOTICE) for the full attribution chain.

The bundled `USPSIMBStandard.ttf` is the USPS-published Intelligent Mail
Barcode font. USPS, Informed Visibility, IMb, IV-MTR, BCG, and Avery are
trademarks of their respective owners.
