# Security Audit — RoadxSolution-mcsearch (standalone Flask app)

- **Date:** 2026-11-21
- **Scope:** `~/workspace/roadxsolution-mcsearch/` — `app.py`, `safer.py`, `db.py`, `commodity.py`, `pickup_lines.py`, `templates/`, `static/`, `requirements.txt`, `Procfile`, `render.yaml`
- **Method:** Read-only code review (no code was changed). All routes, SQL statements, templates, session handling, and external fetches were read. Dependencies were checked with `pip-audit` (15 known vulnerabilities found — see L1).
- **Deployment context assumed:** Public deployment on Render free tier, gunicorn (`web: gunicorn app:app --workers 2 --threads 4 --timeout 120`), SQLite at `data/roadx.db` (ephemeral filesystem — data resets on restart/redeploy), `SECRET_KEY` set by Render (`generateValue: true`).
- **Zack's requirement:** "cyber security ky kio flaw wagera na chor dena — aesay na ho ye hack ho jaye jb live kroun isay" (leave no security flaw — it must not get hacked once live).

## Summary

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 5 |
| Medium | 8 |
| Low | 7 |
| **Total** | **21** |

**Bottom line: do NOT deploy publicly as-is.** Finding C1 alone lets a stranger become admin and take over Zack's account after any restart/redeploy. Fix C1 + H1–H5 first (all are small changes), then the Medium items. The Low items can follow, but the dependency bumps (L1) are a one-line change and should go in the same pass.

---

## Critical

### C1 — Open signup + first-account-is-admin + ephemeral SQLite = anyone can become admin after a restart

- **Files:** `app.py:635-664` (`login()`, signup branch); `db.py` (schema comment on ephemeral storage); `render.yaml` (free tier, no persistent disk)
- **What happens:** Signup is open to the world — no invite, no email verification, no CAPTCHA. The first account in an empty database is made admin:
  ```python
  is_admin = 1 if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0 else 0
  ```
  On Render's free tier the SQLite file is wiped on every restart/redeploy (`db.py` says so itself: "data resets when the service restarts or redeploys"). So after any deploy, the **first stranger who signs up becomes admin**.
- **What an attacker can do with it:** Admin sees every user, every login event (with IPs), and every unused password-reset code (`app.py:1536` admin page), and can set **any** user's password directly (`/admin/set-password`, `app.py:1559`). That is full takeover of Zack's account and every other account, plus the entire carrier database (qualified carriers, notes, scan history).
- **Fix (do all three):**
  1. Disable public signup entirely (remove the signup form, or put it behind an admin-only invite code). Admin creates accounts.
  2. Seed the admin account from environment variables on first run (e.g. `ADMIN_EMAIL` / `ADMIN_PASSWORD`), never by "first signup wins".
  3. Move off ephemeral SQLite before going live — attach a Render persistent disk or switch to Postgres — so the user table (and the admin) survives restarts. Until then, re-check who the admin is after every deploy.

---

## High

### H1 — No rate limiting on login: unlimited password guessing

- **File:** `app.py:635-690` (`login()`)
- **What happens:** There is no rate limit, lockout, CAPTCHA, or delay on login attempts. Failed logins are written to `login_events` but nothing ever reads that table to block anyone. Passwords only need to be 6+ characters (`app.py:649`), so weak passwords are common.
- **What an attacker can do:** Brute-force / credential-stuff Zack's (or any) account with unlimited parallel guesses. Werkzeug's scrypt hashing slows each guess, but with no limit the attacker just keeps going.
- **Fix:** Add `Flask-Limiter` (or equivalent) on `/login` — e.g. 5 attempts/minute per IP, 20/hour, then temporary lockout — plus a CAPTCHA after repeated failures. Enforce a stronger password policy (min 8–10 chars) for new accounts.

### H2 — Password-reset code can be brute-forced (6 digits, no attempt limit)

- **File:** `app.py:700-760` (`forgot()`, `reset_password()`)
- **What happens:** The reset code is a 6-digit number (`"%06d" % secrets.randbelow(1000000)` — 1,000,000 possibilities), valid for 1 hour, and `/reset` has **no attempt limit, no lockout, no delay, no CAPTCHA**. Codes are single-use and expire, which is good, but an attacker can hammer the endpoint with thousands of guesses in parallel.
- **What an attacker can do:** Guess a victim's live reset code and set their password — full account takeover — without ever seeing the admin page. Expected cost is ~500,000 guesses on average; with no throttling that is achievable with parallel requests.
- **Fix:** Rate-limit `/reset` hard (e.g. 5 attempts per email per 15 minutes), invalidate the code after 5 failed tries, and use a longer code (8 digits) or a signed, single-use reset link instead of a short numeric code. Also always show the same message from `/forgot` whether the email exists or not (see L2).

### H3 — No rate limiting on the public search: unauthenticated DoS + upstream IP ban

- **File:** `app.py:597-630` (`/search`), `app.py:512-590` (bulk search, `BULK_MAX_LINES = 50`), `safer.py` (`fetch_snapshot`, `fetch_company_officer`)
- **What happens:** `/search` needs no login and has no rate limit. One bulk search fires up to 50 lines × 2 SAFER fetches (MC + DOT) with 10 parallel workers, plus a Socrata census lookup per detail result. Nothing stops an attacker looping bulk searches forever.
- **What an attacker can do:** Exhaust the Render instance (CPU/threads), and — worse — get FMCSA SAFER and Socrata to rate-limit or ban the server's IP. That kills the app's core feature (live SAFER checks) for every legitimate user. This is also a free, anonymous proxy to hammer FMCSA.
- **Fix:** Rate-limit `/search` per IP (e.g. 30 requests/minute, 5 bulk searches/hour for anonymous users), require login for bulk search, and add a CAPTCHA if abuse appears. Consider caching search results briefly per number.

### H4 — Hardcoded fallback SECRET_KEY published in the public repo

- **File:** `app.py:30` — `app.secret_key = os.environ.get("SECRET_KEY") or "dev-secret-change-me"`
- **What happens:** If `SECRET_KEY` is ever unset, the app silently signs sessions with the literal string `"dev-secret-change-me"` — a value that is committed to the **public** GitHub repo (`github.com/zackalbert12/roadxsolution-mcsearch`). Render sets it via `generateValue: true` today, but one deleted env var, one different host, or one local run in production mode, and every session becomes forgeable.
- **What an attacker can do:** Forge a session cookie for any user id — including the admin — with no password at all. Total account takeover.
- **Fix:** Crash loudly at startup when `SECRET_KEY` is not set (`raise RuntimeError(...)`); never fall back to a default. Rotate the key if it was ever exposed, and keep it out of the repo.

### H5 — Open signup lets anyone burn the scanning infrastructure (resource abuse)

- **File:** `app.py` (`/api/start-scan` — range up to 100,000 MCs; `_motus_page` — up to 5,000 USDOTs per run; `SCAN_WORKERS = 10`)
- **What happens:** Because signup is open (see C1), anyone can create unlimited free accounts and run 100,000-MC scans and 5,000-USDOT Motus runs. Each check hits FMCSA SAFER through rotating proxies with 5–30s backoff sleeps on 429s.
- **What an attacker can do:** Tie up all workers for hours, fill the database with junk, and get the server's IP (or its proxies) banned by SAFER — a denial of service against the real users. There is no per-user daily quota and no approval step.
- **Fix:** Close signup (see C1), cap scan ranges per user per day, add per-account quotas, and log/abuse-monitor scan volume. (The in-process "one scan per user" check is also bypassed by the 2 gunicorn workers — see L5.)

---

## Medium

### M1 — No CSRF protection on any form or API call

- **File:** `app.py` (all POST routes: login, signup, forgot, reset, `/api/start-scan`, `/api/carrier/<id>/star`, `/api/carrier/<id>/call-status`, notes, settings, `/settings/delete-account`, `/admin/set-password`); no CSRF token anywhere; `SESSION_COOKIE_SAMESITE` is not set.
- **What happens:** Every state-changing request relies only on the session cookie. There is no CSRF token (no Flask-WTF, no manual token). Protection today rests entirely on browsers' default SameSite=Lax behaviour, which is not set explicitly and does not cover same-site subdomains, older browsers, or top-level GETs.
- **What an attacker can do:** From a malicious page, trick a logged-in admin into submitting a forged POST — e.g. change a user's password (`/admin/set-password`), delete Zack's account (`/settings/delete-account`), or change settings. The destructive actions need no re-authentication at all.
- **Fix:** Add CSRF tokens to every form/API POST (Flask-WTF `CSRFProtect` is the standard one-line fix), set `SESSION_COOKIE_SAMESITE = "Lax"` explicitly, and require the current password for account deletion and password changes.

### M2 — Sessions are never invalidated when a password changes

- **File:** `app.py` (`settings_page` password section, `reset_password()`, `admin_set_password()`); `current_user()` (`app.py:45-54`) only checks `session["user_id"]`.
- **What happens:** Changing a password — by the user, by reset, or by the admin — does not touch existing sessions. There is no session version, token, or expiry tied to the password.
- **What an attacker can do:** If an attacker has stolen a session cookie (or is logged in on a hijacked session), changing the password does **not** kick them out. The stolen session keeps working for up to 30 days.
- **Fix:** Store a per-user `session_version` (or a random `session_token`) in the users table, embed it in the session at login, verify it in `current_user()`, and bump it on every password change/reset. That instantly kills all other sessions.

### M3 — Session cookie missing Secure and SameSite flags

- **File:** `app.py:31-32` — only `SESSION_COOKIE_HTTPONLY = True` is set.
- **What happens:** The cookie has no `Secure` flag (it can be sent over plain HTTP) and no explicit `SameSite` attribute.
- **What an attacker can do:** On any plain-HTTP access (or a network attacker downgrading the connection), the session cookie is exposed and can be stolen and reused. Missing SameSite also weakens the only CSRF defence the app currently has (see M1).
- **Fix:** `SESSION_COOKIE_SECURE = True`, `SESSION_COOKIE_SAMESITE = "Lax"`. (Render serves HTTPS, so Secure is safe to enable.)

### M4 — No session regeneration on login (session fixation)

- **File:** `app.py:663-664, 683-684` (login/signup set `session["user_id"]` without clearing the session first); `app.py:695, 1512` (logout only pops `user_id`, it does not clear the session).
- **What happens:** The same session cookie that existed before login continues after login. Flask never rotates the session id on authentication.
- **What an attacker can do:** In a session-fixation attack, an attacker plants a known session cookie in a victim's browser; when the victim logs in, that same cookie becomes an authenticated admin/user session the attacker already holds.
- **Fix:** Call `session.clear()` immediately before setting `session["user_id"]` on login/signup, and `session.clear()` on logout.

### M5 — No security headers at all

- **File:** `app.py` — no `@app.after_request` header handling anywhere.
- **What happens:** Responses carry no `X-Frame-Options` / `Content-Security-Policy` / `X-Content-Type-Options` / `Referrer-Policy` headers.
- **What an attacker can do:** Embed the site in an invisible iframe on a malicious page (clickjacking — e.g. trick the admin into clicking "Set" on a password form), and there is no CSP to blunt any future XSS.
- **Fix:** Add an `after_request` hook setting `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a restrictive `Content-Security-Policy` (at minimum `default-src 'self'`, plus the font/CDN hosts the app actually uses).

### M6 — Stored-XSS pattern: scan logs rendered with `innerHTML`

- **File:** `templates/dashboard.html:126` and `templates/motus.html:75` — `body.innerHTML = d.recent_log.map(x => '<tr><td>' + x.mc_number + ... + (x.entity||'') + ... + (x.cargo||'') + ... + (x.note||'') + '</td></tr>')`
- **What happens:** Log rows from the API are injected into the page as raw HTML. Today the fields are either digits, fixed status enums, or SAFER text passed through BeautifulSoup's `get_text()` (which strips markup), so **no live exploit was found in the current data flow** — but the pattern is one field away from stored XSS: any future field that carries user text or uncleaned carrier data (e.g. a legal name in the log) would execute script in every admin/user's browser.
- **What an attacker can do (once a suitable field exists):** Stored XSS — run JavaScript as the victim (steal session cookies are HttpOnly, but the attacker can still act as the user: start scans, change settings, use the admin's session to set passwords).
- **Fix:** Build the rows with `textContent`/`createElement`, or escape every interpolated value with a small `esc()` helper before assigning `innerHTML`. (Same for `dashboard.html:166`, which interpolates the commodity name into `innerHTML` — currently from a server-side list, but escape it anyway.)

### M7 — No request-size limit: unlimited POST bodies

- **File:** `app.py` — `MAX_CONTENT_LENGTH` is never set; no length caps on notes text, username, email, or password fields.
- **What happens:** Flask accepts POST bodies of any size. A note's `text`, a username, or a password can be megabytes long; `generate_password_hash`/`check_password_hash` (scrypt) will happily burn CPU on a giant password.
- **What an attacker can do:** Memory/CPU exhaustion with a few giant requests (cheap DoS), and database bloat via unlimited notes.
- **Fix:** `app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024` (1 MB is generous for this app) and add sane length caps (e.g. username 50, password 128, note text 5,000 chars).

### M8 — No trusted-proxy handling (ProxyFix): client IPs are wrong

- **File:** `app.py` — no `ProxyFix`; `request.remote_addr` is used for `login_events.ip`.
- **What happens:** On Render, TLS is terminated by the platform proxy and the app sees the proxy's IP as `remote_addr`, and `request.is_secure` is False. Every logged "IP" is the Render router, not the real user.
- **What an attacker can do:** Not directly exploitable today, but it silently breaks the only abuse evidence the app keeps (login-event IPs are useless for investigating a break-in), and any IP-based rate limiting added later would throttle/allow the wrong clients.
- **Fix:** `app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)` (Werkzeug) so `remote_addr` and `is_secure` reflect the real client behind Render's proxy.

---

## Low

### L1 — Outdated dependencies with known vulnerabilities (15 findings, `pip-audit`)

- **File:** `requirements.txt` — `Flask==3.1.0`, `requests==2.32.3`, `Werkzeug==3.1.3`, `beautifulsoup4==4.12.3`, `gunicorn==22.0.0`.
- **Findings (from `pip-audit`, 2026-11-21):**
  - Flask 3.1.0 — PYSEC-2026-1377 (fallback signing-key handling; fix 3.1.1) and PYSEC-2026-2151 (missing `Vary: Cookie` — a shared cache could serve one user's page to another; fix 3.1.3).
  - requests 2.32.3 — PYSEC-2026-1872 (.netrc credential leak on crafted URLs; fix 2.32.4) and PYSEC-2026-2275 (predictable temp filename when extracting zips; fix 2.33.0).
  - Werkzeug 3.1.3 — PYSEC-2026-2046 / -2044 / -2320 / -4312 (all `safe_join` Windows device-name bypasses; fix 3.1.4–3.1.9).
- **Applicability:** Most are low risk for this Linux deployment (the Werkzeug issues are Windows-only and the app never calls `send_from_directory`; the app does not use `.netrc` or extract zips). The Flask `Vary: Cookie` one is the most relevant.
- **Fix:** Bump to `Flask>=3.1.3`, `requests>=2.33.0`, `Werkzeug>=3.1.9` (and re-check `beautifulsoup4`/`gunicorn`), then run `pip-audit` in CI on every deploy.

### L2 — Password-reset reveals whether an email has an account

- **File:** `app.py:711` — `error = "No account with that email."` vs. the success message.
- **What an attacker can do:** Enumerate which email addresses have accounts (including Zack's) by trying addresses and watching the message.
- **Fix:** Always return the same message ("If an account exists for that email, a reset was requested…") regardless of whether the email was found.

### L3 — Reset codes stored in plaintext in the database

- **File:** `app.py:713-717`; `db.py` (`password_resets.code TEXT NOT NULL`); `templates/admin.html` (codes shown in plaintext to the admin).
- **What happens:** The one-time code is stored as-is and displayed on the admin page — this is inherent to the admin-relayed design, but it means a database leak (or any admin account) exposes every live reset code.
- **Fix:** Store only a hash of the code (e.g. SHA-256) and compare hashes; the admin relay flow would then need a different design (e.g. the admin generates the code and it is shown once at creation, or switch to emailed reset links). At minimum, document this as an accepted risk and make sure the DB is never exposed (it is not in the repo — see "done right").

### L4 — Error messages leak internal details to anonymous users

- **File:** `app.py:590-596, 620-626` — `"Search failed: %s. Please try again." % exc` and `"Bulk search failed: %s" % exc`; `safer.py` error strings embed proxy URLs and library internals.
- **What an attacker can do:** Learn internal details (proxy addresses, library versions, paths) from exception text — useful for planning further attacks.
- **Fix:** Log the full exception server-side (`app.logger.exception(...)`) and show users a generic message ("Search failed. Please try again.").

### L5 — Run-state and caches live in process memory, but there are 2 gunicorn workers

- **File:** `app.py:131-132` (`RUNNING_SCANS`, `RUNNING_MOTUS`), `safer.py` (`_OFFICER_CACHE`, `_PROXY_POOL`); `Procfile`/`render.yaml` (`--workers 2`).
- **What happens:** The "one scan per user" check (`user["id"] in RUNNING_SCANS`) is per-process, so a user can run two concurrent scans (one per worker). The officer cache is duplicated per worker and grows without bound (one entry per DOT ever searched).
- **Fix:** Run a single worker, or move run-state into the database/Redis; bound `_OFFICER_CACHE` (e.g. max 1,000 entries, LRU) or persist it in SQLite.

### L6 — Small robustness holes that also help attackers

- **File:** `app.py` (`qualified()` / `motus_qualified()`) — `url_for("qualified", export="csv", **request.args)` raises a 500 error if a crafted request already contains an `export` parameter (duplicate keyword argument).
- **File:** `app.py` (`admin_set_password()`) — `int(uid)` on raw form input raises a 500 on non-integer input; also the "new password" field in `templates/admin.html` is `type="text"`, so the password is visible on screen while the admin types it.
- **File:** `app.py:1585` (`if __name__ == "__main__": app.run(...)`) — debug is off by default, but there is no guard: if `FLASK_DEBUG=1` is ever set in the environment, the Werkzeug debugger (remote code execution) would be enabled.
- **File:** `app.py:663-664` — signup sets `session.permanent = True` unconditionally, so a brand-new account gets a 30-day session even without ticking "Remember me".
- **Fix:** Filter `request.args` before passing to `url_for`; validate `uid` with a try/except (or a form check); change the admin password input to `type="password"`; explicitly pass `debug=False` and refuse to start if `FLASK_DEBUG` is set; make signup respect the Remember-me choice like login does.

### L7 — Admin password changes are not logged (no audit trail)

- **File:** `app.py` (`admin_set_password()`) — updates the hash and flashes a message; nothing is written to any log table.
- **What happens:** If an admin account is abused (or an admin goes rogue), there is no record of who changed whose password and when.
- **Fix:** Insert a row into a small audit table (admin id, target user id, action, timestamp, IP) on every admin password change and on every reset-code use, and show it on the admin page.

---

## What is already done right (do not break these)

- **SQL injection: none found.** Every query uses `?` placeholders with bound parameters. The only `%`-formatted SQL uses hardcoded table names (`export_data()`, `delete_account()`) and an `ORDER BY` chosen from a whitelist dict (`_query_carriers()`), never user input. LIKE patterns are parameterised too.
- **Passwords are hashed properly.** Werkzeug `generate_password_hash` / `check_password_hash` (scrypt with per-password salt). No plaintext passwords anywhere.
- **No XSS in templates.** Jinja autoescaping is on everywhere; there is no `|safe`, `render_template_string`, or `Markup` in any template or Python file. Reflected values (`q`, error messages, carrier fields) are all escaped.
- **Access control is enforced per object.** `login_required` / `admin_required` guard every protected route, and the mutation endpoints (`/api/carrier/<id>/star`, `/api/carrier/<id>/call-status`, `/notes/<id>/delete`, `/errors/<id>/retry`) all filter by `user_id` — no IDOR was found: a logged-in user cannot read or change another user's carriers, notes, scans, settings, or error MCs. All list/export/report queries are scoped with `WHERE user_id=?`.
- **Session cookie is HttpOnly** (`app.py:31`), Remember-me is capped at 30 days, and unticked logins are browser-session scoped.
- **No debug mode in the deployed path** — `app.run()` is only the `__main__` fallback; Render runs gunicorn. Flask's default error pages do not leak tracebacks.
- **Database is not in the public repo.** `.gitignore` excludes `data/*.db` and `.env`; verified with `git ls-files` — no database, no `.env`, no secrets committed.
- **External fetches are safe by construction.** SAFER and Socrata URLs are fixed HTTPS hosts; user input is passed as URL-encoded query parameters (no SSRF — the host cannot be changed by input). TLS verification is on by default, so the free proxies used for SAFER cannot read or modify response content. `search_by_name` uses the fixed `query_param=NAME` endpoint.
- **Reset codes are random and single-use.** `secrets.randbelow`, 1-hour expiry, marked `used=1` after a successful reset, and looked up with a parameterised query.
- **No dangerous Python calls.** No `eval`, `exec`, `os.system`, `subprocess`, `pickle`, or `yaml.load` anywhere in the app.
- **Input validation where it matters.** Scan ranges are capped (max 100,000 MCs), Motus runs capped at 5,000 USDOTs, bulk search capped at 50 lines, sort order is whitelisted, call-status is whitelisted, phone reverse lookup needs 7+ digits, and the public search deliberately stores no carrier email addresses.
- **Admin powers are at least gated.** `/admin` and `/admin/set-password` both require `admin_required` (checked against the DB on every request), and the admin page itself says who can see it.

---

## Fix order before going live (checklist)

1. **C1** — Close public signup; seed admin from env; get persistent storage (disk/Postgres).
2. **H4** — Fail startup when `SECRET_KEY` is unset; rotate the key.
3. **H1 + H2** — Rate-limit `/login` and `/reset`; invalidate reset codes after failed attempts.
4. **H3 + H5** — Rate-limit public `/search` (login for bulk); per-user scan quotas.
5. **M1 + M3** — CSRF tokens; `Secure` + `SameSite=Lax` cookies.
6. **M2 + M4** — Session versioning + `session.clear()` on login/logout.
7. **M5 + M7 + M8** — Security headers; `MAX_CONTENT_LENGTH`; `ProxyFix`.
8. **M6** — Escape the `innerHTML` log rendering.
9. **L1–L7** — Dependency bumps, enumeration fix, generic error messages, audit logging, small robustness fixes; re-run this audit after the fixes.
