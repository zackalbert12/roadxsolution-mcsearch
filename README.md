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
  never stored plain). The first account created becomes the site **admin**.
  Sessions via Flask session cookie.
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
# open http://127.0.0.1:5000 — Sign Up first (that account becomes admin)
```

Optional env vars: `PORT` (default 5000), `SECRET_KEY` (set a long random
value in production), `ROAD_DB_PATH` (SQLite file location).

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
3. Deploy. The free URL will be
   **https://roadxsolution-mcsearch.onrender.com** (or
   `roadxsolutions-mcsearch.onrender.com` if that name is taken — rename in
   `render.yaml`).
4. Open the URL, **Sign Up** — the first account becomes the admin.
   A paid custom domain can be attached later in Render → Settings →
   Custom Domain.

> **Free-tier note:** Render's free filesystem is ephemeral — the SQLite
> database (`data/roadx.db`) resets when the service restarts or
> redeploys, so users, scans and notes do not survive a restart. For
> durable storage, attach a Render persistent disk and set `ROAD_DB_PATH`
> to a path on it, or move to a hosted database. Also note the free tier
> sleeps after inactivity, so the first load after a while is slow.

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
