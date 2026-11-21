# RoadxSolution-mcsearch

A standalone, deployable copy of the TSS MC-scanning dispatcher dashboard
(`tss-qdyl.onrender.com`), rebuilt from a read-only reverse-engineering
exploration. It scans FMCSA MC-number ranges **live against SAFER**
(safer.fmcsa.dot.gov), keeps authorised carriers whose cargo matches the
selected types, and gives call lists, notes and pickup-line scripts.

Branded **RoadxSolution-mcsearch** everywhere — not "Truck Services Solutions"
(the source app's name).

## Features

- **Sign Up / Log In** — email + username + password (hashed with Werkzeug,
  never stored plain; minimum 8 characters). Signup is open, but it never
  creates an admin: the admin account is seeded from the
  `ROAD_ADMIN_EMAIL` / `ROAD_ADMIN_PASSWORD` environment variables at
  startup (see "Security" below). Sessions via Flask session cookie
  (HttpOnly, Secure, SameSite=Lax); changing a password signs out every
  other session, and logging out signs out every device.
- **Forgot password** — user requests a reset; the admin sees the one-time
  code in **Admin → Password Reset Requests** and relays it; the user sets a
  new password with it. The admin can also set any user's password directly.
- **Admin page** (admin only) — all signed-up users (signup date, last
  login, login count), login-activity feed, password reset requests.
- **Dashboard** — MC-range scan (Start/End MC), Start/Stop, live progress
  (checked, qualified, errors, elapsed, time left, speed, status), live log
  (latest 25), Download CSV, cargo-type guide, pickup-line card. The scan
  runs as a server-side background thread, so it keeps going across page
  loads.
- **Live SAFER checks** — each MC's Company Snapshot is fetched live and
  parsed (legal name, DBA, entity type, authority status, cargo carried,
  address, city, state, phone, power units). Classified as:
  `Qualified` / `Not Found` / `Skip — Not authorized` /
  `Skip — Record inactive` / `Skip — Cargo mismatch` / `Error`.
  Polite to SAFER: sequential fetches with a short delay, 20 s timeouts,
  backoff on HTTP 429/503/5xx.
- **Qualified Carriers** — search, state filter, cargo filter, **commodity
  filter**, sort, CSV export, star/save, call-status tracking
  (Call / Call Back / Do Not Call).
- **Commodity filter** — select a commodity (e.g. **Electronics**) and see
  the MCs of carriers whose trucks haul it, using the equipment→commodity
  mapping in `commodity.py` matched against each carrier's SAFER cargo.
- **Motus Search / Motus Register** — paste USDOT numbers; each is checked
  live against SAFER with the same rules. (There is no public FMCSA
  AuthHist / Daily Register feed, so pasted USDOTs are the input.)
- **Restarted Carriers** — carriers seen NOT AUTHORIZED in an earlier check
  and AUTHORIZED in a later check, from this app's own check history.
- **Saved MCs** (starred), **Notes** (MC, title, text, tags, priority,
  reminder date), **Pickup Lines** (category chips + copy), **Scan History**,
  **Error MCs** with retry, **Reports** (totals, qualification rate, cargo
  tallies, top states, 14-day activity), **Settings** (scanner defaults,
  cargo types, theme, notifications, export data, delete account), **About**.

## Honest differences from the original

- Motus date-range register/AuthHist pulls have no public FMCSA feed, so
  those pages check USDOT numbers you paste (same live SAFER check).
- Restarted Carriers is detected from this site's own check history.
- Scan speed: checks run in parallel (10 workers by default) and rotate
  through proxies, like the original — measured ~205 checks/min direct
  locally (was ~1 check/sec sequential). The original's ~400/min uses
  proxies too; ours falls back to direct fetching when no proxy works.
- Qualification rule here is transparent: AUTHORIZED for-hire carrier whose
  cargo matches a selected type. The original had an extra, undisclosed
  server-side check (some authorised + general-freight carriers were
  qualified:false there).

## Run locally

```bash
cd roadxsolution-mcsearch
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000 — Sign Up to create a normal account.
# To get an admin account locally, set ROAD_ADMIN_EMAIL and
# ROAD_ADMIN_PASSWORD before starting (see "Security" below).
```

Optional env vars: `PORT` (default 5000), `SECRET_KEY` (set a long random
value in production — if unset, a random key is generated for that process
only and sessions will not survive a restart), `ROAD_DB_PATH` (SQLite file
location), `ROAD_ADMIN_EMAIL` / `ROAD_ADMIN_PASSWORD` (seed the admin
account), `SESSION_COOKIE_SECURE` (`1` by default; set to `0` only for
plain-HTTP local testing), `ROAD_SOCRATA_APP_TOKEN` (free Socrata token —
raises the owner-name lookup limit).

Scan speed (like the original site, which uses proxies): checks run in
parallel (default 10 workers — `ROAD_SCAN_WORKERS` to tune) and SAFER
requests rotate through proxies. Proxies: set `ROAD_PROXIES` to a
comma-separated list of `http://host:port` proxies (your own or a paid
service); otherwise the app tries a free public proxy list
(`ROAD_PROXY_LIST_URL` to override it) and falls back to direct fetching
when no proxy works — a dead proxy list can never stall a scan.
`ROAD_NO_PROXY=1` disables proxies entirely. Measured locally:
~205 checks/min with 10 workers direct (was ~60/min sequential).
Note: stopping a scan mid-chunk and resuming can skip a few MCs (under
10); scans are otherwise exact.

## Deploy on Render (free tier)

1. Push this folder to a GitHub repo.
2. In Render: **New → Web Service → Build and deploy from a Git repo**,
   pick the repo. Render detects `render.yaml` (service name
   `roadxsolution-mcsearch`, free plan) — or set it manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 120`
   - `SECRET_KEY` is generated by Render (`generateValue: true`).
   - `ROAD_ADMIN_EMAIL` is set in `render.yaml`; set `ROAD_ADMIN_PASSWORD`
     in the Render dashboard (Environment → Add Environment Variable) —
     never commit the password anywhere.
3. Deploy. The free URL will be
   **https://roadxsolution-mcsearch.onrender.com** (or
   `roadxsolutions-mcsearch.onrender.com` if that name is taken — rename in
   `render.yaml`).
4. Open the URL and log in with the seeded admin account
   (`ROAD_ADMIN_EMAIL` / `ROAD_ADMIN_PASSWORD`). Regular users sign up
   normally — signup never creates an admin. A paid custom domain can be
   attached later in Render → Settings → Custom Domain.

> **Free-tier note:** Render's free filesystem is ephemeral — the SQLite
> database (`data/roadx.db`) resets when the service restarts or
> redeploys, so users, scans and notes do not survive a restart. For
> durable storage, attach a Render persistent disk and set `ROAD_DB_PATH`
> to a path on it, or move to a hosted database. Also note the free tier
> sleeps after inactivity, so the first load after a while is slow.

## Security

Fixed 2026-11-21 (audit: `SECURITY_AUDIT.md`, 21 findings — all 21
addressed; re-audit after any big change):

- **Admin takeover (C1):** the admin account is seeded from
  `ROAD_ADMIN_EMAIL` / `ROAD_ADMIN_PASSWORD` at startup when the database
  has no admin; it is never overwritten, and signup never creates an
  admin — so a SQLite wipe on restart cannot hand the admin to a stranger.
- **Secret key (H4):** no hardcoded fallback. If `SECRET_KEY` is unset a
  random key is generated for that process only (with a loud warning).
- **Rate limits (H1/H2/H3/H5):** login 5/min + 20/hour per IP; reset-code
  guesses 5 per email per 15 min and the live code is invalidated after 5
  failed guesses; public search 30/min per IP, bulk search 10/hour
  (anonymous) or 30/hour (logged in); scans and Motus runs 10 starts per
  user per hour. Over the limit returns HTTP 429.
- **CSRF (M1):** every POST form and fetch call carries a per-session
  token; `SameSite=Lax` is set explicitly.
- **Sessions (M2/M3/M4):** a per-user session version is checked on every
  request and bumped on any password change/reset and on logout, so old
  sessions die immediately (a stolen cookie stops working the moment the
  password changes or the user logs out); sessions are cleared and
  re-created on login (no fixation); the cookie is HttpOnly, Secure (by
  default) and SameSite=Lax.
- **Headers (M5):** `X-Frame-Options: DENY`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, and a restrictive CSP that
  still allows the app's own inline scripts.
- **XSS (M6):** scan-log rows and the commodity note are built with
  escaped values / DOM text nodes, never raw `innerHTML`.
- **Limits (M7):** `MAX_CONTENT_LENGTH` is 10 MB; usernames, passwords and
  note fields have length caps.
- **Proxy (M8):** `ProxyFix` is on, so logged IPs and `is_secure` are the
  real client behind Render's proxy.
- **Dependencies (L1):** Flask 3.1.3, requests 2.33.0, Werkzeug 3.1.9.
- **Small fixes (L2/L4/L5/L6/L7):** the forgot-password page never reveals
  whether an email has an account; anonymous error messages are generic
  (details go to the server log); the one-scan / one-Motus-run check is
  enforced in the database so it holds across both gunicorn workers; the
  officer cache is bounded at 1,000 entries; the admin password field is
  `type="password"`, non-integer user ids no longer 500, the app refuses
  to start with `FLASK_DEBUG` set, and signup respects the Remember-me
  choice; every admin password change and reset-code use is written to an
  audit log shown on the Admin page.

Accepted risks (documented, not fixed — they are inherent to the design):

- **Reset codes are stored in plaintext** (L3) because the admin must be
  able to read and relay the code. They are single-use, expire in 1 hour,
  and are invalidated after 5 failed guesses. The database is never in
  the repo and is only reachable by the server.
- **Rate limits are in process memory** — with 2 gunicorn workers an
  attacker gets twice the allowance, and limits reset on restart.
- **Free-tier SQLite is ephemeral** — after a restart the admin is
  re-seeded from the env vars, but users, scans and notes are gone
  unless a persistent disk / hosted database is attached.

## Project layout

```
app.py            Flask app: routes, auth, scan/motus background engines
safer.py          Live SAFER fetch + parse + classify
commodity.py      Equipment→commodity mapping + cargo-keyword matching
pickup_lines.py   Pickup-line library
db.py             SQLite schema + connection helper
templates/        Jinja pages (base sidebar layout + one per page)
static/style.css  Original-style design (white sidebar, orange accents)
requirements.txt  Flask, gunicorn, requests, beautifulsoup4
Procfile          gunicorn entrypoint
render.yaml       Render free-plan service definition
```
