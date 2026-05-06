# mailtrace

Generate USPS first-class mail envelopes with a printed Intelligent Mail
Barcode (IMb), batch-print sticker sheets in advance, and track each piece
through USPS Informed Visibility — for the price of a normal stamp, no
Certified Mail required.

This is a re-engineered re-implementation of [1997cui/envelope][upstream]
with modern tooling, type hints, tests, and a small Dockerfile. Releases
are published as multi-arch container images to GHCR
(`ghcr.io/co1lin/mailtrace`) so consumers only need a compose file.

[upstream]: https://github.com/1997cui/envelope

## Features

- **Multi-user, invite-only** — first-run setup at `/setup` creates the
  initial admin in the browser. The admin portal then handles invites,
  edits, force-reset (random or chosen password), promote/demote,
  deactivate/reactivate, and hard-delete with confirmation. Per-user
  Mailer ID so users never collide on `(MID, serial)` keyspace.
- **Stock-management lifecycle** — pieces flow through
  `generated → printed → in_flight → delivered`, with `archived` as a
  soft-delete orthogonal to the main flow. Generate IMbs in advance for
  known recipients (e.g. 5 stickers for a friend), print them onto an
  Avery sheet (auto-marks `printed`), peel-and-mail one when sending
  (mark `mailed` to start tracking), watch USPS confirm delivery.
- **Address book** per user, with sender / recipient / both roles, and an
  in-page "Validate against USPS" button on every form.
- **Mail pieces** — single-piece form (default: mail now), stock-batch
  form (one row per recipient × count, default: hold as stock), and
  CSV import. Each piece has a durable scan history.
- **Sticker sheet** — pick N pieces and a starting cell on an Avery 8163
  sheet, get a multi-page PDF that lays them out and paginates
  automatically. Generating the PDF auto-flips selected `generated`
  pieces to `printed`.
- **Background poller** — periodic on-demand pulls from USPS IV per piece,
  with an age-aware cadence (15 min for fresh, decaying to 6 h for stale),
  exponential backoff on errors, and auto-archive after 60 d.
- **USPS Informed Visibility (IV-MTR) push receiver** at `POST /usps_feed`
  — HTTP Basic Auth, gzip support, raw-payload archive for replay,
  tolerant field-name parsing, per-delivery audit log. Configured in the
  admin portal at `/admin/ingest`. Works alongside the poller.
- **Email digests** — opt-in per user; whenever new scans arrive, the
  user gets a single digest email summarizing all updates. SMTP is
  configured in the admin portal at `/admin/email`, with copy-paste field
  guides for Office 365, Gmail, and transactional relays.
- **Envelope generator** — #10 envelope (PDF/HTML) and Avery 8163 sticker
  layouts, with the bundled USPS IMb font embedded inline.
- **Address standardization** via the new USPS API at `apis.usps.com`.
- **First-class observability** — `/healthz` checks Redis + DB, structured
  errors, OpenAPI docs at `/docs`.

## Configuration philosophy

mailtrace is a **multi-tenant platform**. Two distinct roles configure
different things, and the README is split accordingly:

| Role | Where they configure | What |
|------|----------------------|------|
| **Host / platform admin** (the person running the docker compose) | env vars + `/admin/*` | One secret in env (`SESSION_SECRET`) plus DB/Redis URLs and infra paths. Everything else operational lives in the admin portal: users, SMTP, IV-MTR push receiver, poll cadence. **The host does NOT manage USPS credentials.** |
| **Tenant user** (each individual signed-up user) | `/auth/account` | Their own USPS API Client ID/Secret, BCG account (optional), Mailer ID, notification preferences. |

USPS credentials are per-user by design — quota, rate limits, and data
attribution are all scoped per developer.usps.com app and per BCG
account, so two users sharing the platform never share USPS keyspace
or queries. The host can't see or set those credentials.

There is no required CLI step in either role.

## Quick start (standalone)

Pull the published image — no clone required:

```bash
mkdir mailtrace && cd mailtrace
curl -fLO https://raw.githubusercontent.com/Co1lin/mailtrace/main/docker-compose.yml
curl -fL  https://raw.githubusercontent.com/Co1lin/mailtrace/main/.env.example -o .env
$EDITOR .env             # fill in 3 secrets
docker compose up -d
```

**The only env var the host must set:**

```env
# Signs session cookies. 64 random hex chars.
# Generate with: python -c 'import secrets; print(secrets.token_hex(32))'
MAILTRACE_SESSION_SECRET=...
```

Everything else has a working default. **USPS API credentials are not
the host's concern** — each tenant user enters their own at
`/auth/account` after logging in, scoped to their account.

See [`.env.example`](.env.example) for the full bootstrap-env list and
[USPS credentials — what each tenant user needs and how to get each](#usps-credentials--what-each-tenant-user-needs-and-how-to-get-each)
for what to point users at after onboarding.

The app listens on `127.0.0.1:8084` by default; override the publish via
`MAILTRACE_BIND` (e.g. `MAILTRACE_BIND=0.0.0.0:8080` for direct exposure,
or leave it as localhost when fronted by a reverse proxy on the host).

### First-run (host's job)

1. Visit `http://127.0.0.1:8084/` (or wherever you bound).
2. You're redirected to `/setup`. Enter the platform admin email and
   password. Submit. The `/setup` page disappears as soon as any user
   exists, so it's not a hijack vector after the box is up.
3. Log in. As host, you should now configure platform-level services:
   - `/admin/email` — point to an SMTP server (Office 365, Gmail,
     SendGrid, etc.) so digest emails can go out. Click *Send test*.
   - `/admin/ingest` — generate a Basic Auth password for the IV-MTR
     push receiver if you want push-tracking (see
     [Configuring the USPS push feed](#configuring-the-usps-push-feed)).
   - `/admin/settings` — adjust poll cadence if needed (the defaults are
     fine for most homelab setups).
4. Invite each tenant user from `/admin/`. Each gets a one-time
   temporary password they must change on first login.

### Onboarding (each tenant user's job)

After login, each user is taken through the setup page at `/auth/account`:

1. **Mailer ID** (required) — their own USPS-issued MID from
   gateway.usps.com.
2. **USPS API credentials** (optional, recommended) — their own
   developer.usps.com Client ID + Secret. Unlocks the "Validate against
   USPS" button. Click *Test USPS API* on the page to verify.
3. **BCG credentials** (optional) — only if they want pull-tracking.
   Skip if the platform's push feed is configured. Click *Test BCG*
   to verify.
4. **Notifications** (optional) — opt in to email digests when scans
   arrive. Click *Send test notification* to verify the SMTP path
   works end-to-end.

Once at least the Mailer ID is set, the user can create pieces.

## Quick start (multi-service / Caddy / behind a reverse proxy)

This is the typical homelab / pve deployment: mailtrace + Redis as one
stanza inside a larger compose file, with Caddy terminating TLS and
fanning out to several internal services on a shared docker network.

```yaml
# excerpt from a multi-service compose.yml
services:
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    networks: [edge]

  mailtrace:
    image: ghcr.io/co1lin/mailtrace:latest
    restart: unless-stopped
    depends_on:
      mailtrace-redis: {condition: service_healthy}
    environment:
      # Only ONE secret has to come from env — everything else (USPS API
      # creds, BCG creds, poll cadence, SMTP, push feed) is configured
      # at runtime in the admin portal.
      MAILTRACE_SESSION_SECRET: ${MAILTRACE_SESSION_SECRET}
      MAILTRACE_REDIS_URL: redis://mailtrace-redis:6379/0
      MAILTRACE_HOST: 0.0.0.0
      MAILTRACE_PORT: 8080
      MAILTRACE_INGEST_ARCHIVE_DIR: /data/ingest_raw
      # caddy is the only host whose XFF we trust for source-IP logging.
      # Use the docker subnet your edge network sits on.
      MAILTRACE_TRUSTED_PROXIES: '["172.18.0.0/16"]'
      MAILTRACE_TIMEZONE: America/Los_Angeles
    volumes:
      - mailtrace-data:/data
    networks: [edge]
    # No ports: Caddy reaches it on the docker network.

  mailtrace-redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes"]
    volumes: [mailtrace-redis-data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
    networks: [edge]

networks:
  edge:
volumes:
  mailtrace-data:
  mailtrace-redis-data:
  caddy-data:
```

…and the `Caddyfile`:

```caddyfile
mailtrace.example.com {
    reverse_proxy mailtrace:8080
}
```

That's it for the proxy side — Caddy terminates TLS automatically with
Let's Encrypt and forwards to the container by service name. The
`/usps_feed` endpoint authenticates inside the app with Basic Auth, so
Caddy doesn't need any special config for it.

## Configuring the USPS push feed

Once the app is up and reachable on the public internet, in `/admin/ingest`:

1. Pick a Basic Auth username (e.g. `usps_iv`).
2. Click **Rotate** to generate a strong password — copy it now, it's
   shown once.
3. Tick **Enable receiver** and **Save**.
4. (Optional) Tick **Archive raw payloads** — strongly recommended; raw
   files land in `/data/ingest_raw/YYYY/MM/DD/` and let you replay a
   broken parse without waiting on USPS.
5. Hit **Run self-test** to confirm the auth + parse path works end-to-end.

Then, in the USPS IV-MTR portal, create a Subscription's Delivery target:

| USPS portal field | Value |
|---|---|
| Protocol Type | `HTTPS JSON` |
| Host Address | your public hostname (no scheme), e.g. `mailtrace.example.com` |
| Port | `443` |
| Host Target Directory | `/usps_feed` |
| Host User Name | the username from above |
| Host Password | the password from **Rotate** |
| File Format | `JSON` |
| File Transfer Format | `Un-zipped` (or `Zipped` if you flip "Expect gzip" on) |
| Frequency | per your IV agreement (commonly 1 hour) |

USPS' source IPs are not fixed/published — that's why we authenticate by
Basic Auth, not IP allowlist. The receiver returns `503` (not `404`)
when disabled, so toggling the switch doesn't lose deliveries — USPS
retries on its next cycle.

`/admin/ingest` shows the last 20 deliveries with byte-count, record-count,
matched/orphan counts, and the archived file path. Auth failures show up
as `failed` rows for forensics.

## Configuring email notifications

In `/admin/email`:

1. Pick your SMTP server (the page has copy-paste field guides for
   Office 365, Gmail, and transactional relays like SendGrid / Postmark /
   Resend / SES).
2. Save and click **Send test email** to verify.
3. Each user opts in on their own **Account** page (`/auth/account`) by
   ticking "Email me when scans arrive."
4. Whenever the poller (or push feed) adds new scans for a user's pieces,
   they get one digest email summarizing all the updates.

## Endpoints

| Route | Method | Auth | Description |
|---|---|---|---|
| `/setup` | GET/POST | public (only when DB has 0 users) | First-run admin setup |
| `/` | GET | session | Dashboard with recent pieces |
| `/auth/login` | GET/POST | public | Sign in |
| `/auth/account` | GET/POST | session | Per-user setup: MID, USPS API Client ID/Secret, BCG creds, notifications |
| `/auth/account/test-usps-api` | POST | session | Probe the user's USPS API creds (Test button) |
| `/auth/account/test-bcg` | POST | session | Probe the user's BCG creds (Test button) |
| `/auth/account/test-notify` | POST | session | Send a test notification to the user's notify address |
| `/admin/` | GET | admin session | User management |
| `/admin/settings` | GET/POST | admin session | Platform poll cadence + auto-archive horizon |
| `/admin/email` | GET/POST | admin session | SMTP config + test send |
| `/admin/ingest` | GET/POST | admin session | USPS push feed config + recent log + self-test |
| `/addresses/` | GET / POST / `/{id}` | session | Address book CRUD |
| `/addresses/validate` | POST | session | USPS address standardization |
| `/pieces/` | GET | session | List with filter tabs (Stock / Printed / In flight / Delivered / Archived) |
| `/pieces/new` | GET/POST | session | Single-piece create |
| `/pieces/batch` | GET/POST | session | Stock batch (recipient × count) |
| `/pieces/import` | GET/POST | session | CSV bulk import |
| `/pieces/{id}` | GET | session | Detail page with scans + lifecycle actions |
| `/pieces/{id}/mark-printed` | POST | session | Transition `generated → printed` |
| `/pieces/{id}/mark-mailed` | POST | session | Transition to `in_flight`, start polling |
| `/pieces/{id}/{archive,unarchive,delete,refresh}` | POST | session | Per-piece actions |
| `/pieces/bulk-action` | POST | session | Bulk mark-printed/mark-mailed/archive/delete |
| `/pieces/sheet/setup`, `/pieces/sheet` | GET, POST | session | Sticker-sheet picker + render (PDF auto-marks printed) |
| `/usps_feed` | POST | **HTTP Basic Auth** | IV-MTR push receiver |
| `/healthz` | GET | public | Liveness (pings Redis and DB) |
| `/docs` | GET | session | OpenAPI / Swagger UI |

## Concurrency model

mailtrace runs as `MAILTRACE_WORKERS` uvicorn processes (default 2).
HTTP requests fan out across all workers in parallel — heavy operations
like PDF rendering on one worker never block a `Validate against USPS`
click handled by another. Background work (the per-piece poll, the
auto-archive sweep, the notification digest) runs in **only one worker
at a time** via a Redis-backed leader lock:

- On every loop tick (default every 5 minutes), each worker tries to
  acquire-or-renew the lock at `mailtrace:bg_loop:leader` with a TTL of
  2x the loop interval.
- The first worker to acquire it becomes leader, runs the cycle, and
  keeps renewing on each tick. Other workers see "not yours" and just
  sleep until the next tick.
- If the leader crashes between renewals, the lock TTL expires and the
  next worker to wake up acquires it cleanly. Failover is automatic
  with up to one cycle of latency (default 5 min).
- On normal worker shutdown, the leader proactively releases the lock
  so a peer can take over instantly.

This means you get HTTP parallelism without duplicating background
work. Per-user OAuth tokens are also cached in Redis under per-user
keys (`mailtrace:usps:user:{id}:...`), so credential rotation for one
user never invalidates anyone else's cached token.

The push receiver at `/usps_feed` is fully concurrent across workers —
USPS POSTs land on whichever worker the OS picks, and per-scan
`(scan_date_time, event_code, machine_name, facility_zip,
facility_locale)` dedup on `Scan.dedup_hash` makes the writes
idempotent. Even if USPS retries the same delivery (because we 5xx'd
once) the scans are inserted exactly once.

## How tracking works

USPS scanners read the IMb during sortation and emit a stream of events.
mailtrace gets those two ways:

1. **Pull** (per-user, optional): the background poller calls the IV
   piece-tracking endpoint per piece on an age-aware cadence, using the
   *piece owner's* BCG credentials (set at `/auth/account`). Pieces
   whose owner has no BCG creds set are silently skipped.
2. **Push** (platform-wide, lower latency): IV-MTR's "Data Delivery"
   feature POSTs JSON files to the `/usps_feed` endpoint on a schedule
   (e.g. every 1 hour). The host configures the receiver in
   `/admin/ingest` *once*, then each user creates their own USPS-side
   subscription pointing at the same endpoint. Events arrive multiplexed
   by IMb; mailtrace routes each event to the right user's piece.

The two paths share the same `ingest_scan` code path. Scans are
deduplicated by `(scan_date_time, event_code, machine_name, facility_zip,
facility_locale)` so duplicate deliveries (USPS retrying after a 5xx, or
a scan appearing in both the push file and the next pull) only insert
one row.

A scan landing on a stock piece (`generated` / `printed`) auto-promotes
it to `in_flight` — USPS already has it, so we should track.

**Token caching:** OAuth tokens for both APIs are cached in Redis under
per-user keys (`mailtrace:usps:user:{user_id}:modern:*` and
`...:iv:*`). One user's credential rotation never invalidates anyone
else's cached token.

## USPS credentials — what each tenant user needs and how to get each

> **Audience:** this section is for tenant users (people with an account
> on a mailtrace deployment), not the host. As host, you don't manage
> USPS credentials — direct your users to this section after onboarding
> them.
>
> **Where to paste them:** every user enters their own USPS credentials
> at **`/auth/account`** after logging in. They are not in env vars and
> are not visible to the host or other users.

USPS exposes mailtrace's needs across **two completely separate API
platforms** with **two completely different auth schemes**. There's no
single credential that does everything. Here's why, and what you actually
need.

### Two APIs, two auth schemes

| | apis.usps.com (modern) | iv.usps.com (legacy IV-MTR) |
|---|---|---|
| **Auth** | OAuth2 `client_credentials` with Client ID + Client Secret | OAuth2 `password` grant with BCG username + password |
| **Portal** | <https://developer.usps.com/> | <https://gateway.usps.com/> |
| **What it does** | Address standardization (Addresses 3.0); parcel tracking by tracking-number (Tracking 3.0) | IMb scan-history pull (the only way to *pull* first-class scan events by IMb) |
| **mailtrace uses it for** | the "Validate against USPS" button | the background pull-tracking poller |

USPS hasn't migrated IMb scan-history to the modern platform — Tracking 3.0
is for parcel tracking numbers, not IMb-keyed first-class scan streams.
**That's why we need the literal BCG username/password** for pull-tracking;
it's a USPS-side limitation, not our choice. The good news is there's a
fully-modern alternative described next.

### Choose your tracking strategy first

Before grabbing creds, decide which path you want — that determines
which env vars you actually need to set.

- **Push (recommended, modern):** USPS POSTs scan events to your
  `/usps_feed` endpoint on a schedule. Auth is HTTP Basic Auth between
  USPS and *us* (configured in `/admin/ingest`); no USPS-side credential
  lives in your env. Lower latency, no plaintext-password-in-env smell.
  Setup: see [Configuring the USPS push feed](#configuring-the-usps-push-feed).
- **Pull (legacy fallback):** mailtrace's poller calls
  `iv.usps.com/ivws_api/.../piece/imb/{imb}` per piece on an age-aware
  cadence. Authenticates with your BCG username/password. Use this only
  if you can't get push set up.
- **Both:** fine — they share the same `ingest_scan` code path with
  per-scan dedup, so there's no double-counting.

If you go push-only, you only need creds (1) below. If you also want
pull, add (3).

### (1) USPS API Client ID + Secret

**What:** OAuth2 client credentials for USPS' modern API at `apis.usps.com`.

**Used for:** the "Validate against USPS" button on the piece / address
forms (calls Addresses 3.0). Without these, the button returns an error
but the rest of the app still works — users can type addresses by hand.

**How to get them:**

1. Make sure you have a **BCG account** first (see (2)) — the developer
   portal uses BCG single sign-on.
2. Go to <https://developer.usps.com/> and click **Get Started**;
   follow the prompts. You'll end up at the USPS Customer Onboarding
   Portal at <https://cop.usps.com/>, signed in with your BCG account.
3. Inside cop.usps.com, look for a small **My Apps** button in the
   top-right corner. Click it.
4. Create a new app (any name — e.g. "mailtrace"). Once the app is
   created, its detail page exposes a **Consumer Key** and a
   **Consumer Secret**.
5. Paste the Consumer Key into the **Client ID** field at
   `/auth/account` in mailtrace, and the Consumer Secret into the
   **Client Secret** field. Click **Save all sections**, then click
   **Test USPS API** under "Test your setup" to verify.
6. Wait ~5–10 minutes after USPS app creation for their edge to
   propagate the credentials. First-time client_credentials calls
   sometimes return 401 for ~10 minutes after app creation; the
   Test USPS API button will then return success.

> **Note:** USPS' onboarding flow has changed in the past and the
> exact button labels / screens may differ from the above. The goal
> is unchanged: end up with a **Consumer Key + Consumer Secret** pair
> from a USPS-issued developer app, then paste them into mailtrace's
> Account page. Address standardization is one of the products bundled
> with the default app on the modern API; the deprecated "Web Tools"
> (XML) flow at `secure.shippingapis.com` is *not* what mailtrace uses.

**Cost:** free tier is generous; mailtrace makes one call per Validate
click and one OAuth refresh every ~30 minutes.

### (2) BCG account at gateway.usps.com (prerequisite for everything)

**What:** the **Business Customer Gateway** is USPS' enrolment portal.
The login here (a) issues your **Mailer ID** (MID), (b) provisions
IV-MTR access for the push feed, and (c) is the SSO identity used by
developer.usps.com.

**You don't put these creds anywhere unless you also want pull-tracking
(see (3))**, but you absolutely need an account.

**How to get one:**

1. Go to <https://gateway.usps.com/> → **Sign Up**.
2. Pick **Business Account**. For homelab / personal use, register
   yourself as a sole-proprietor — your name + home address is fine.
3. Confirm via the email USPS sends. You can sign in immediately after.
4. Inside BCG, go to *Mailing Services* and request access to:
   - **Mailer ID** — your MID is automatically issued; copy the
     6- or 9-digit number from the *Mailer ID* dashboard. Each mailtrace
     user enters their own MID on `/auth/account`.
   - **Informed Visibility – Mail Tracking & Reporting (IV-MTR)** —
     usually approved within a day. This unlocks both push (Data
     Delivery) and pull (legacy IV API) features.

### (3) BCG username + password *(optional — pull-tracking only)*

**What:** the literal username and password of your BCG account from (2).
Yes, real credentials — USPS' legacy IV-MTR pull API uses an OAuth2
`password` grant against `services.usps.com`. There is no API-key
equivalent; USPS hasn't migrated this surface. If that's a deal-breaker
for you, ask the platform host to enable the push feed instead and
leave these unset.

**Where they go:** paste into the **BCG username** and **BCG password**
fields at `/auth/account` (your own setup page — no other user sees
them). They're stored on your User row in the platform DB. Click **Test
BCG** after saving to verify; the test result is recorded next to the
field.

**How to get them:** they're whatever you typed into the Sign Up form
at gateway.usps.com. There's no separate generation step. If you forgot,
reset your BCG password and use the new one.

**When to skip:** if the platform admin has configured the push feed
at `/admin/ingest`, leave both blank. Your scans will arrive via the
shared push receiver and the per-user pull poller will simply skip
your pieces (your own deliveries via push still work fine; only the
pull-only fallback is disabled).

**Operational notes if you use them:**
- Token caching is in Redis under per-user keys, so rotating one user's
  BCG password never invalidates anyone else's tokens.
- Rotating means changing your BCG password and updating the field
  at `/auth/account`. No separate revocation surface — the next token
  refresh (within 30 min) picks up the new credentials.
- The platform DB lives on a Docker volume with a "homelab + single
  trusted host operator" threat model. If you don't trust your platform
  host with BCG-level access to your USPS account, prefer the push feed
  path — the host then never sees your USPS credentials at all.

The original Chinese write-up at
<https://blog.ctyi.me/%E7%94%9F%E6%B4%BB/2021/06/03/USPS_IV_MTR.html>
walks through the full BCG/IV-MTR enrolment in detail.

## Local development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run pre-commit install
uv run pytest                       # ~100 tests, no network
uv run ruff check . && uv run mypy src
uv run mailtrace                    # serves on http://0.0.0.0:8080
```

For PDF generation locally, install `wkhtmltopdf` (e.g. `apt install
wkhtmltopdf` or `brew install --cask wkhtmltopdf`). The Docker image
already bundles it.

## Project layout

```
src/mailtrace/
├── app.py             # FastAPI factory + lifespan (background poller)
├── config.py          # MAILTRACE_* env settings (pydantic-settings)
├── auth.py            # Session-cookie auth + bcrypt
├── middleware.py      # AuthMiddleware: login redirect, first-run gate
├── db.py              # Async SQLAlchemy engine + session
├── models.py          # ORM: User, Address, MailPiece, Scan, AppConfig,
│                      #      SmtpConfig, IngestSubscription, IngestLog
├── services.py        # create_piece, ingest_scan, poll loop, notifications
├── store.py           # Redis: serial allocator
├── usps.py            # USPS API client (oauth + tracking + addresses)
├── imb.py             # Intelligent Mail Barcode encoder
├── mail.py            # SMTP sender (aiosmtplib)
├── pdf.py             # wkhtmltopdf options
├── cli.py             # `mailtrace admin` typer commands (optional)
├── routes/
│   ├── main.py        # /, /healthz
│   ├── setup.py       # /setup (first-run only)
│   ├── auth.py        # /auth/{login,logout,change-password,account}
│   ├── admin.py       # /admin/* (users + SMTP + ingest)
│   ├── ingest.py      # /usps_feed receiver (Basic Auth, gzip, archive)
│   ├── addresses.py   # /addresses/* CRUD + /validate
│   └── pieces.py      # /pieces/* (CRUD + lifecycle + sheet + import)
├── static/USPSIMBStandard.ttf
└── templates/         # Jinja2 templates (per route group)

tests/                 # pytest, fakeredis, ASGI transport (no network)
Dockerfile             # multi-stage, slim, non-root, healthcheck
docker-compose.yml     # app + redis with named volume
```

## License & attribution

Licensed under the **GNU Affero General Public License v3** (see
[`LICENSE`](LICENSE)) to honor the upstream license. The IMb encoder is
adapted from the original Python implementation by Sam Rushing
(Simplified BSD); see [`NOTICE`](NOTICE) for the full attribution chain.

The bundled `USPSIMBStandard.ttf` is the USPS-published Intelligent Mail
Barcode font. USPS, Informed Visibility, IMb, IV-MTR, BCG, and Avery are
trademarks of their respective owners.
