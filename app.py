"""RoadxSolution-mcsearch — standalone Flask app.

A dispatcher MC-verification workspace: scan FMCSA MC ranges live against
SAFER, keep qualified carriers, Motus USDOT checks, notes, pickup lines,
reports and admin user management. Deployable on Render's free tier.
"""
import concurrent.futures
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    jsonify, Response, abort, flash,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

# pyotp is in requirements.txt. Guard the import so a stale environment
# without it still boots — the 2FA routes refuse with a clear message
# instead of crashing the whole app.
try:
    import pyotp
except ImportError:  # pragma: no cover - missing dependency
    pyotp = None

import db as store
from safer import check_mc, check_usdot, search_by_name, fetch_company_officer
from commodity import (COMMODITIES, EQUIPMENT_COMMODITIES, commodity_names,
                        equipment_label, expected_trucks,
                        filter_carriers_by_commodity)
from pickup_lines import PICKUP_LINES, CATEGORIES, lines_for

app = Flask(__name__)
# H4: no hardcoded fallback key. Audit fix 3 — if SECRET_KEY is unset,
# refuse to start in production: with 2 gunicorn workers a random key
# per process would sign sessions differently, and ~half of all
# CSRF-protected POSTs would 403 nondeterministically. Production is
# RENDER set, or ALLOW_INSECURE_DEV not explicitly "1". Local dev
# (no RENDER + ALLOW_INSECURE_DEV=1) may keep a generated key.
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _is_production = bool(os.environ.get("RENDER")) or \
        os.environ.get("ALLOW_INSECURE_DEV") != "1"
    if _is_production:
        raise SystemExit(
            "Refusing to start: SECRET_KEY is not set. Set it in production"
            " (Render generates one via render.yaml) — without it, each"
            " worker signs sessions with a different key and logins and"
            " form submissions fail at random.")
    _secret = secrets.token_hex(32)
    print("WARNING: SECRET_KEY is not set — generated a random key for this"
          " process only. Set SECRET_KEY in production.", flush=True)
app.secret_key = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure by default (Render serves HTTPS). Set SESSION_COOKIE_SECURE=0 only
# for plain-HTTP local testing.
app.config["SESSION_COOKIE_SECURE"] = \
    os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# M7: reject oversized request bodies outright.
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
# M8: Render terminates TLS at its proxy — trust X-Forwarded-For/Proto so
# request.remote_addr and request.is_secure are the real client.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

store.init_db()

# ---------------------------------------------------------------- helpers

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_time():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- demo trials
# A new signup gets a 7-day full trial (plan 'Demo'). Only the admin can
# give Lifetime access. Since the owner-set access durations (Zack
# 2026-11-21), WHEN access ends lives in users.access_expires_at — one
# field for every time-limited plan (Demo 7 days, 1 month, 6 months,
# 1 year, custom N months); NULL = Lifetime, never expires. The timer
# starts the moment the owner picks the duration. Timestamps are naive
# local time, same as now_str().
DEMO_TRIAL_DAYS = 7
DEMO_EXPIRED_MESSAGE = ("Your 7-day demo has ended. $5/month to continue — "
                        "contact the owner (WhatsApp +92 339 4126983, email "
                        "zackalbert.dispatch@gmail.com).")
DEMO_CLOSED_MESSAGE = ("Your 7-day demo has ended and your account has been "
                       "closed. Contact the owner for $5/month access — "
                       "WhatsApp +92 339 4126983, email "
                       "zackalbert.dispatch@gmail.com.")
DEMO_BLOCKED_MESSAGE = ("Your demo has ended. $5/month to continue — "
                        "contact the owner (WhatsApp +92 339 4126983, email "
                        "zackalbert.dispatch@gmail.com).")
# Same $5/month billing + owner contact, for a time-limited plan that is
# not the Demo trial (1 month / 6 months / 1 year / custom): the wording
# says "your access" instead of "your 7-day demo", because the demo
# wording would be wrong for those users.
ACCESS_EXPIRED_MESSAGE = ("Your access has ended. $5/month to continue — "
                          "contact the owner (WhatsApp +92 339 4126983, "
                          "email zackalbert.dispatch@gmail.com).")
ACCESS_CLOSED_MESSAGE = ("Your access has ended and your account has been "
                         "closed. Contact the owner for $5/month access — "
                         "WhatsApp +92 339 4126983, email "
                         "zackalbert.dispatch@gmail.com.")
# Owner-set access durations (Admin page popup): choice -> days from the
# moment the owner picks it. A month is 30 days, so custom 1.5 months
# is 45 days. Custom durations are capped at 120 months in the route.
ACCESS_DURATIONS = {"demo": 7, "1month": 30, "6months": 180,
                    "1year": 365}
ACCESS_PLAN_LABELS = {"demo": "Demo", "1month": "1 month",
                      "6months": "6 months", "1year": "1 year",
                      "lifetime": "Lifetime", "custom": "Custom"}
# Owner block (Zack 2026-11-21): the admin can end any user's access.
# Separate from the closed-Demo blocklist above — different message,
# different list, and a blocked user keeps their data and plan.
BLOCKED_MESSAGE = ("Your access has been blocked by the owner. Contact "
                   "the owner (WhatsApp +92 339 4126983, email "
                   "zackalbert.dispatch@gmail.com).")
DEMO_SCAN_LIMIT = 10000
DEMO_SCAN_LIMIT_MESSAGE = ("Demo allows at most 10,000 MC numbers per scan;"
                           " ask the owner for Lifetime for larger scans.")


def _demo_scan_too_big(user, start_mc, end_mc):
    """True when a Demo user tries to scan more than DEMO_SCAN_LIMIT MCs.

    Owner (admin) and Lifetime accounts have no limit. Enforced at the
    scan start API endpoints — the only ways run_scan is launched.
    """
    if user["is_admin"] or user["plan"] != "Demo":
        return False
    return end_mc - start_mc + 1 > DEMO_SCAN_LIMIT


def _access_expiry(user):
    """The datetime a user's access ends, or None (never expires).

    Reads users.access_expires_at — the single source of truth. One
    safety net for rows written before that column existed: a Demo
    user with no expiry falls back to created_at + 7 days, which is
    exactly what the init_db backfill writes, so the fallback can
    never disagree with the stored field."""
    try:
        raw = user["access_expires_at"]
    except (KeyError, TypeError, IndexError):
        raw = None
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
    try:
        if user["plan"] == "Demo":
            created = datetime.strptime(user["created_at"],
                                        "%Y-%m-%d %H:%M:%S")
            return created + timedelta(days=DEMO_TRIAL_DAYS)
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    return None


def _format_access_end(dt):
    """'21 Dec 2026' — the human date shown for an access end."""
    return dt.strftime("%d %b %Y") if dt else None


def _future_str(days):
    """now + N days, as a '%Y-%m-%d %H:%M:%S' timestamp string."""
    return (datetime.now() + timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _demo_status(user):
    """(expired, days_left) for a time-limited account.

    Lifetime users and the admin never expire: (False, None). The
    expiry comes from access_expires_at for every plan — Demo (7
    days), 1 month, 6 months, 1 year, custom. days_left is the whole
    days remaining, rounded up, so the last day still reads as 1 day
    left. Kept the old name because every call site (login, scan
    guards, banners, background threads) already uses it."""
    try:
        is_admin = user["is_admin"]
        plan = user["plan"]
    except (KeyError, TypeError, IndexError):
        return (False, None)
    if is_admin or plan == "Lifetime":
        return (False, None)
    expiry = _access_expiry(user)
    if expiry is None:
        return (False, None)
    remaining = expiry - datetime.now()
    if remaining.total_seconds() <= 0:
        return (True, 0)
    days_left = remaining.days + (1 if remaining.seconds else 0)
    return (False, days_left)


def _expiry_message(user):
    """The 'your access ended, $5/month' message for this user's plan:
    the Demo wording for Demo users, the generic access wording for
    every other time-limited plan."""
    try:
        if user["plan"] == "Demo":
            return DEMO_EXPIRED_MESSAGE
    except (KeyError, TypeError, IndexError):
        pass
    return ACCESS_EXPIRED_MESSAGE


def _expiry_closed_message(user):
    """The 'account has been closed' message for this user's plan —
    Demo wording for Demo users, generic access wording otherwise."""
    try:
        if user["plan"] == "Demo":
            return DEMO_CLOSED_MESSAGE
    except (KeyError, TypeError, IndexError):
        pass
    return ACCESS_CLOSED_MESSAGE


def _is_blocked(user):
    """True when the owner's block flag is set on this user row."""
    try:
        return bool(user["blocked"])
    except (KeyError, TypeError, IndexError):
        return False


def _demo_expired_for(user_id):
    """Re-read a user from the DB and say whether their access has
    expired. Used by background threads, which must stop mid-run when
    access ends."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT plan, created_at, access_expires_at, is_admin"
            " FROM users WHERE id=?",
            (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return True
    expired, _ = _demo_status(dict(row))
    return expired


def _delete_user_data(conn, uid):
    """Delete one user's rows from every user-owned table, including
    the users row itself. Shared by self-service delete-account and
    the demo auto-close, so both wipe exactly the same tables."""
    for table in ("carriers", "scans", "scan_log", "check_history",
                  "error_mcs", "notes", "motus_runs", "login_events",
                  "password_resets", "settings"):
        conn.execute("DELETE FROM %s WHERE user_id=?" % table, (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))


def _close_expired_demo(user):
    """Auto-close an account whose access has ended: blocklist the
    email (so it can never sign up again), delete its data and clear
    the session. Covers every time-limited plan — Demo, 1 month,
    6 months, 1 year, custom — because _demo_status reads
    access_expires_at for all of them. Admin and Lifetime users are
    never closed. Returns True when an account was closed.
    Self-service delete does NOT blocklist — it calls
    _delete_user_data directly, never this function."""
    expired, _ = _demo_status(user)
    if not expired:
        return False
    conn = store.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO closed_demos (email, user_id, closed_at)"
            " VALUES (?,?,?)",
            (user["email"].lower(), user["id"], now_str()))
        _delete_user_data(conn, user["id"])
        conn.commit()
    finally:
        conn.close()
    session.clear()
    return True


# ---------------------------------------------------------------- security

MIN_PASSWORD_LEN = 8
# Password strength (already enforced everywhere a password is set):
# signup, /reset, Settings change-password, admin set-password and
# admin create-user all require MIN_PASSWORD_LEN–128 characters.
# The admin's own password comes from the ROAD_ADMIN_PASSWORD env var
# and is never checked or changed here.

DEMO_PASSWORD_LEN = 16


def _random_demo_password():
    """Cryptographically random demo password (letters + digits).

    Used when the admin creates a demo account without typing a
    password — every demo account then gets its own password instead
    of sharing one known one. Only the hash is stored; the plaintext
    is shown to the admin once, right after creation."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet)
                     for _ in range(DEMO_PASSWORD_LEN))
        if (any(c.isalpha() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def _totp_enabled(user):
    """True when opt-in TOTP 2FA is enabled for this user (admin only).
    The secret lives in users.totp_secret; NULL means 2FA is off."""
    return bool(user and user["totp_secret"])


def _totp_verify(secret, code):
    """Verify a 6-digit TOTP code against a base32 secret. valid_window=1
    allows one 30 s step of clock skew either way. Returns False when
    pyotp is unavailable (fail closed) or the code is malformed."""
    if not pyotp or not secret:
        return False
    code = (code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(code, valid_window=1))
    except Exception:
        return False


# --- email OTP login -------------------------------------------------------
# Passwordless sign-in: a 6-digit code is emailed to the user's
# registered address. The code is stored HASHED (werkzeug hash, like
# passwords), expires after 10 minutes, is single-use, and only the
# newest live code per user works. Sending needs SMTP configured via
# environment (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM);
# with no SMTP config the request fails with a clear message — the
# code is never shown on screen and never logged.
OTP_EXPIRY_MINUTES = 10
OTP_MAX_REQUESTS = 5          # OTP requests per email per 15 minutes
OTP_REQUEST_WINDOW = 900
OTP_MAX_ATTEMPTS = 5          # wrong-code guesses before the live code is cancelled
OTP_VERIFY_WINDOW = 900       # lockout window for OTP-code guesses
OTP_NOT_CONFIGURED_MESSAGE = ("Email login is not set up yet — ask the site "
                              "owner to configure email sending, or sign in "
                              "with your password.")
# Zack 2026-11-21 22:41: the 6-digit codes (signup verification AND
# login OTP) are sent FROM zackalbert.dispatch@gmail.com — never any
# other address. The sender is fixed here, not an env override: the
# SMTP credentials for this Gmail arrive at deploy time, and Gmail
# only delivers mail whose From matches the authenticated account.
# SMTP_FROM is intentionally NOT read — a stray env value must never
# change where the codes come from.
CODE_SENDER_EMAIL = "zackalbert.dispatch@gmail.com"
SIGNUP_VERIFY_NOT_CONFIGURED_MESSAGE = ("Email verification is not set up "
    "yet — ask the site owner to configure email sending, or try again "
    "later.")


def _smtp_config():
    """SMTP settings from the environment, or None when not configured.
    The sender is always CODE_SENDER_EMAIL (see above) — SMTP_FROM is
    not read, so a deploy-time env value can never change it."""
    host = (os.environ.get("SMTP_HOST") or "").strip()
    port = os.environ.get("SMTP_PORT") or "587"
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = os.environ.get("SMTP_PASS") or ""
    sender = CODE_SENDER_EMAIL
    if not host or not user or not password:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return {"host": host, "port": port, "user": user,
            "password": password, "from": sender}


def _send_login_otp_email(to_email, code):
    """Send the 6-digit login code. Returns (True, None) on success,
    (False, message) when email is not configured or sending fails.
    The code is never logged."""
    cfg = _smtp_config()
    if cfg is None:
        return (False, OTP_NOT_CONFIGURED_MESSAGE)
    msg = EmailMessage()
    msg["Subject"] = "Your RoadxSolutions login code"
    msg["From"] = cfg["from"]
    msg["To"] = to_email
    msg.set_content(
        "Your RoadxSolutions login code is: %s\n\n"
        "It expires in %d minutes and can be used once. "
        "If you did not request this code, ignore this email.\n"
        % (code, OTP_EXPIRY_MINUTES))
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # plain SMTP relay (e.g. local) — continue without TLS
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception:
        return (False, "Could not send the login code right now. Try again "
                       "later, or sign in with your password.")
    return (True, None)


def _send_signup_verify_email(to_email, code):
    """Send the 6-digit signup verification code. Returns (True, None)
    on success, (False, message) when email is not configured or
    sending fails. The code is never logged."""
    cfg = _smtp_config()
    if cfg is None:
        return (False, SIGNUP_VERIFY_NOT_CONFIGURED_MESSAGE)
    msg = EmailMessage()
    msg["Subject"] = "Your RoadxSolutions signup code"
    msg["From"] = cfg["from"]
    msg["To"] = to_email
    msg.set_content(
        "Your RoadxSolutions signup verification code is: %s\n\n"
        "Enter it to finish creating your account. It expires in %d "
        "minutes and can be used once. If you did not request this "
        "code, ignore this email.\n"
        % (code, OTP_EXPIRY_MINUTES))
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # plain SMTP relay (e.g. local) — continue without TLS
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    except Exception:
        return (False, "Could not send the verification code right now. "
                       "Try again later.")
    return (True, None)


def _otp_fails(email):
    """Failed OTP-code guesses recorded for `email` (shared count)."""
    conn = store.connect()
    try:
        row = conn.execute("SELECT fails FROM otp_failures WHERE email=?",
                           (email,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _otp_fails_inc(email):
    """Record one more failed OTP-code guess; returns the new total."""
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO otp_failures (email, fails, updated_at)"
            " VALUES (?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET fails=fails+1,"
            " updated_at=excluded.updated_at",
            (email, 1, time.time()))
        conn.commit()
        row = conn.execute("SELECT fails FROM otp_failures WHERE email=?",
                           (email,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _otp_fails_clear(email):
    """Forget the failed-guess count (correct code used / new code sent)."""
    conn = store.connect()
    try:
        conn.execute("DELETE FROM otp_failures WHERE email=?", (email,))
        conn.commit()
    finally:
        conn.close()


# --- rate limiting (H1/H2): counters live in SQLite (rate_limit_events /
# reset_failures tables, db.py) so the 2 gunicorn workers share ONE
# count. Audit fix 9 — the old in-memory dicts gave an attacker 2x the
# allowance and let ~10 reset-code guesses through across workers.
_RATE_MAX_WINDOW = 3600  # longest window in use (login-h); prune older rows


def _rate_limited(key, limit, window):
    """True when `key` has already used `limit` calls in the last
    `window` seconds; otherwise records this call and returns False.
    `key` is a tuple (namespace, identifier) — stored as one string."""
    now = time.time()
    k = ":".join(str(p) for p in key)
    conn = store.connect()
    try:
        # Prune rows older than any window we use, then count this
        # key's attempts inside the window. The DELETE takes the write
        # lock first, so concurrent workers serialise here.
        conn.execute("DELETE FROM rate_limit_events WHERE ts <= ?",
                     (now - _RATE_MAX_WINDOW,))
        n = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE key=? AND ts > ?",
            (k, now - window)).fetchone()[0]
        if n >= limit:
            conn.commit()
            return True
        conn.execute("INSERT INTO rate_limit_events (key, ts) VALUES (?,?)",
                     (k, now))
        conn.commit()
        return False
    finally:
        conn.close()


def _reset_fails(email):
    """Failed reset-code guesses recorded for `email` (shared count)."""
    conn = store.connect()
    try:
        row = conn.execute("SELECT fails FROM reset_failures WHERE email=?",
                           (email,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _reset_fails_inc(email):
    """Record one more failed reset-code guess; returns the new total."""
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO reset_failures (email, fails, updated_at)"
            " VALUES (?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET fails=fails+1,"
            " updated_at=excluded.updated_at",
            (email, 1, time.time()))
        conn.commit()
        row = conn.execute("SELECT fails FROM reset_failures WHERE email=?",
                           (email,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _reset_fails_clear(email):
    """Forget the failed-guess count (correct code used / code expired)."""
    conn = store.connect()
    try:
        conn.execute("DELETE FROM reset_failures WHERE email=?", (email,))
        conn.commit()
    finally:
        conn.close()


# --- CSRF (M1): one token per session, sent as a hidden form field or an
# X-CSRF-Token header on fetch calls --------------------------------------
def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def _csrf_valid():
    tok = session.get("_csrf")
    if not tok:
        return False
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not sent:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            sent = data.get("csrf_token")
    return bool(sent) and hmac.compare_digest(str(sent), str(tok))


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _csrf_token}


@app.before_request
def _csrf_check():
    if request.method == "POST" and not _csrf_valid():
        if request.path.startswith("/api/"):
            return jsonify({"ok": False,
                            "error": "Session expired — reload the page and try again."}), 403
        abort(403)


@app.before_request
def _close_expired_demo_accounts():
    """Auto-close: as soon as a Demo trial ends, the account is deleted.
    Runs on every authenticated request, so a trial ending mid-session
    closes the account on the next request. Admin and Lifetime users
    are never touched. Auth pages are excluded — the login route closes
    an expired account itself, but only after the password checks out
    (a wrong-password attempt must not delete anything)."""
    if request.endpoint in ("login", "login_totp", "login_otp",
                            "login_otp_request", "login_otp_verify",
                            "signup_verify", "signup_verify_resend",
                            "forgot", "reset_password", "static"):
        return None
    if not session.get("user_id"):
        return None
    user = current_user()
    if not user:
        return None
    expired, _ = _demo_status(dict(user))
    if not expired:
        return None
    closed_message = _expiry_closed_message(dict(user))
    _close_expired_demo(dict(user))
    flash(closed_message)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": closed_message}), 403
    return redirect(url_for("login"))


@app.before_request
def _refuse_blocked_users():
    """Owner block: a blocked user's sessions die the moment the admin
    blocks them (session_version bump, same mechanism as a password
    change), but refuse here too so any still-valid session is killed
    on its next request. Blocked users keep their data and plan —
    only access is disabled — and unblock restores it."""
    if request.endpoint in ("login", "login_totp", "login_otp",
                            "login_otp_request", "login_otp_verify",
                            "signup_verify", "signup_verify_resend",
                            "forgot", "reset_password", "static"):
        return None
    if not session.get("user_id"):
        return None
    user = current_user()
    if not user or not _is_blocked(user):
        return None
    session.clear()
    flash(BLOCKED_MESSAGE)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": BLOCKED_MESSAGE}), 403
    return redirect(url_for("login"))


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'")
    return resp


# --- friendly error pages (audit fix 2): the old defaults were Flask's
# bare "Forbidden / Not Found / Internal Server Error" pages with no way
# back. Rendering a template runs _inject_csrf, which re-issues a valid
# CSRF token, so a 403 can be fixed by one reload.
@app.errorhandler(403)
def _handle_403(err):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False,
                        "error": "Session expired — reload the page and try again."}), 403
    # Delete-account double-submit: the first submit already deleted the
    # account and cleared the session (which also wipes the CSRF token),
    # so a retry of the same form no longer matches any token and lands
    # here. The account is gone, not "unchanged" — send the user to the
    # login page with the honest outcome instead of the scary
    # "Session expired / nothing was changed" page. CSRF still blocked
    # the request; only the landing page changes, and only for a browser
    # that just deleted an account (the delete route marks the fresh
    # session with account_deleted). A still-logged-in user with a stale
    # token keeps the normal 403 page below, so a genuinely stale token
    # stays visible, and a random logged-out POST gets the normal 403.
    if (request.method == "POST"
            and request.path == "/settings/delete-account"
            and session.get("account_deleted")):
        queued = [m for _c, m in session.get("_flashes", [])]
        if "Your account has been deleted." not in queued:
            flash("Your account has been deleted.")
        return redirect(url_for("login"))
    if request.path.startswith("/admin") and current_user():
        title = "Admin only"
        message = "Only the site admin can open this page."
        show_reload = False
    else:
        title = "Session expired"
        message = ("Your session expired, or this page was opened before the "
                   "site restarted. Reload the page and try again.")
        show_reload = True
    return render_template("error_page.html", code=403, title=title,
                           message=message, show_reload=show_reload,
                           user=current_user()), 403


@app.errorhandler(404)
def _handle_404(err):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Not found."}), 404
    return render_template("error_page.html", code=404,
                           title="Page not found",
                           message="This page does not exist, or it was moved.",
                           show_reload=False,
                           user=current_user()), 404


@app.errorhandler(500)
def _handle_500(err):
    app.logger.exception("Unhandled error on %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False,
                        "error": "Something went wrong. Please try again."}), 500
    return render_template("error_page.html", code=500,
                           title="Something went wrong",
                           message=("Please reload the page and try again. If it "
                                    "keeps happening, let the site admin know."),
                           show_reload=True,
                           user=current_user()), 500


# --- admin seeding (C1): the admin comes from env vars, NEVER from
# "first signup wins" -----------------------------------------------------
def _seed_admin():
    """Create (or promote) the admin account from ROAD_ADMIN_EMAIL /
    ROAD_ADMIN_PASSWORD when the database has no admin yet. Never
    overwrites an existing admin's password. Safe to run in every
    worker — the second one just finds the admin already there."""
    email = os.environ.get("ROAD_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ROAD_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    conn = store.connect()
    try:
        if conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone():
            return
        existing = conn.execute(
            "SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET is_admin=1 WHERE id=?",
                         (existing["id"],))
            conn.commit()
            print("Seeded admin: promoted existing user %s to admin."
                  % email, flush=True)
            return
        username = (email.split("@")[0] or "admin")[:50]
        base, n = username, 1
        while conn.execute("SELECT id FROM users WHERE username=?",
                           (username,)).fetchone():
            n += 1
            username = "%s%d" % (base, n)
        try:
            conn.execute(
                "INSERT INTO users (email, username, password_hash, is_admin,"
                " created_at) VALUES (?,?,?,?,?)",
                (email, username, generate_password_hash(password), 1,
                 now_str()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()  # another worker seeded first
            return
        print("Seeded admin account %s from ROAD_ADMIN_EMAIL."
              % email, flush=True)
    finally:
        conn.close()


_seed_admin()


def _bump_session_version(user_id):
    """Invalidate every existing session for a user (M2) — called on any
    password change, reset, or admin-set password."""
    conn = store.connect()
    try:
        conn.execute("UPDATE users SET session_version=session_version+1"
                     " WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _log_admin_action(admin_id, target_user_id, action):
    """Audit trail for admin password changes and reset-code use (L7)."""
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO admin_audit (admin_id, target_user_id, action, ip,"
            " created_at) VALUES (?,?,?,?,?)",
            (admin_id, target_user_id, action, request.remote_addr, now_str()))
        conn.commit()
    finally:
        conn.close()


def _login_user(user, remember):
    """Fresh session on login/signup (M4): clear any pre-login session,
    then store the user id and the session version (M2).

    Audit fix 1: the CSRF token survives the clear. A login/signup form
    rendered before this login keeps working — without this, its token
    died with the old session and the submit 403'd."""
    csrf = session.get("_csrf")
    session.clear()
    if csrf:
        session["_csrf"] = csrf
    session["user_id"] = user["id"]
    session["sv"] = user["session_version"]
    session.permanent = bool(remember)


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    # M2: a session whose version no longer matches was killed by a
    # password change — the user must log in again.
    if session.get("sv") != row["session_version"]:
        return None
    return row


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def get_settings(user_id):
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            # Audit fix 6: INSERT OR IGNORE — two simultaneous first
            # requests for a new user used to race and the second 500'd
            # on the PRIMARY KEY. Re-select afterwards either way.
            conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def selected_cargo(settings):
    out = []
    if settings.get("cargo_general"):
        out.append("general")
    if settings.get("cargo_fresh"):
        out.append("fresh")
    if settings.get("cargo_reefer"):
        out.append("reefer")
    return tuple(out) or ("general", "fresh", "reefer")


def carrier_dict(row):
    d = dict(row)
    # Audit fix 10: a malformed stored value (only reachable by direct
    # DB edits) used to 500 every list/CSV request for that user.
    for key in ("cargo_carried", "cargo_categories"):
        try:
            d[key] = json.loads(d[key] or "[]")
        except (ValueError, TypeError):
            d[key] = []
    return d


def _log_check(conn, user_id, scan_id, rec):
    conn.execute(
        "INSERT INTO scan_log (user_id, scan_id, mc_number, status, entity,"
        " power_units, cargo, checked_at, note) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, scan_id, rec["mc_number"], rec["status"],
         rec.get("entity_type") or "", rec.get("power_units") or "",
         ", ".join(rec.get("cargo_carried") or []), now_time(),
         rec.get("note") or ""))
    conn.execute(
        "INSERT INTO check_history (user_id, mc_number, authority_status, checked_at)"
        " VALUES (?,?,?,?)",
        (user_id, rec["mc_number"], rec.get("authority_status") or "",
         now_str()))


# ---------------------------------------------------------------- scan engine

RUNNING_SCANS = {}   # user_id -> {"stop": Event, "thread": Thread}
RUNNING_MOTUS = {}   # user_id -> {"stop": Event, "thread": Thread}

# Scan speed (like the original site): several MCs are checked at once
# and SAFER requests rotate through proxies — about 5-7 checks/sec
# instead of ~1/sec. Tune with ROAD_SCAN_WORKERS (default 10).
SCAN_WORKERS = max(1, int(os.environ.get("ROAD_SCAN_WORKERS", "10")))
_DB_WRITE_LOCK = threading.Lock()  # serialise SQLite writes from workers


def mark_stale_scans_paused(user_id):
    """A scan whose DB row still says 'running' but has no live thread is a
    leftover from an earlier session (e.g. the server restarted mid-scan).
    It must never restart on its own — mark it Paused so it only continues
    when the user presses Resume Scan."""
    if user_id in RUNNING_SCANS:
        return
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE scans SET status='Paused' WHERE user_id=? AND status='running'",
            (user_id,))
        conn.commit()
    finally:
        conn.close()


def _failed_rec(identifier, exc):
    """Error-shaped record for a check that raised unexpectedly — one bad
    MC must never kill the scan."""
    return {"mc_number": str(identifier), "status": "Error",
            "note": "Check failed: %s" % exc, "qualified": False,
            "fetch_error": True, "error": str(exc), "entity_type": "",
            "usdot_number": "", "legal_name": "", "dba_name": "",
            "authority_status": "", "cargo_carried": [],
            "cargo_categories": [], "address_line1": "", "city": "",
            "state": "", "phone": "", "power_units": ""}


def _safe_check_mc(mc, cargo_types):
    try:
        return check_mc(mc, cargo_types)
    except Exception as exc:
        return _failed_rec(mc, exc)


def _safe_check_usdot(usdot, cargo_types):
    try:
        return check_usdot(usdot, cargo_types)
    except Exception as exc:
        return _failed_rec(usdot, exc)


def _write_scan_result(user_id, scan_db_id, scan_id, rec):
    """Persist one finished check: log + qualified/error rows + counters.

    Runs under the write lock — SQLite writes from parallel workers must
    not interleave. current_mc tracks the highest MC finished so far
    (resume continues from there; a stop mid-chunk can skip a few MCs).
    """
    mc = int(rec["mc_number"])
    with _DB_WRITE_LOCK:
        conn = store.connect()
        try:
            _log_check(conn, user_id, scan_id, rec)
            if rec["qualified"]:
                conn.execute(
                    "INSERT INTO carriers (user_id, scan_id, source, mc_number,"
                    " usdot_number, legal_name, dba_name, entity_type,"
                    " authority_status, cargo_carried, cargo_categories,"
                    " address_line1, city, state, phone, power_units, checked_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, scan_id, "scan", rec["mc_number"],
                     rec.get("usdot_number") or "", rec.get("legal_name") or "",
                     rec.get("dba_name") or "", rec.get("entity_type") or "",
                     rec.get("authority_status") or "",
                     json.dumps(rec.get("cargo_carried") or []),
                     json.dumps(rec.get("cargo_categories") or []),
                     rec.get("address_line1") or "", rec.get("city") or "",
                     rec.get("state") or "", rec.get("phone") or "",
                     rec.get("power_units") or "", now_str()))
            if rec["status"] == "Error":
                conn.execute(
                    "INSERT INTO error_mcs (user_id, scan_id, source, mc_number,"
                    " error, checked_at) VALUES (?,?,?,?,?,?)",
                    (user_id, scan_id, "scan", rec["mc_number"],
                     rec.get("note") or "Fetch error", now_str()))
            conn.execute(
                "UPDATE scans SET current_mc=MAX(COALESCE(current_mc,0),?),"
                " checked=checked+1, qualified=qualified+?, errors=errors+?,"
                " not_found=not_found+? WHERE id=?",
                (mc, 1 if rec["qualified"] else 0,
                 1 if rec["status"] == "Error" else 0,
                 1 if rec["status"] == "Not Found" else 0, scan_db_id))
            conn.commit()
        finally:
            conn.close()


def _count_scan_write_error(user_id, scan_db_id, scan_id, rec):
    """Best-effort fallback when _write_scan_result itself fails: count
    that MC as an error so the scan keeps going and the user sees it.
    Never raises — a failure here must not kill the scan either."""
    mc = str(rec.get("mc_number") or "")
    try:
        mc_int = int(mc)
    except (TypeError, ValueError):
        mc_int = 0
    try:
        with _DB_WRITE_LOCK:
            conn = store.connect()
            try:
                conn.execute(
                    "INSERT INTO error_mcs (user_id, scan_id, source,"
                    " mc_number, error, checked_at) VALUES (?,?,?,?,?,?)",
                    (user_id, scan_id, "scan", mc,
                     "Could not save this check — please retry this MC.",
                     now_str()))
                conn.execute(
                    "UPDATE scans SET current_mc=MAX(COALESCE(current_mc,0),?),"
                    " checked=checked+1, errors=errors+1 WHERE id=?",
                    (mc_int, scan_db_id))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        app.logger.exception("Scan %s: could not record write error for MC %s",
                             scan_id, mc)


def run_scan(user_id, scan_db_id, scan_id, start_mc, end_mc, cargo_types,
             fleet=(None, None)):
    """Background thread: walk an MC range, checking each against SAFER.

    Checks run in parallel (ROAD_SCAN_WORKERS threads, default 5) with
    rotating proxies — about 5-7 checks/sec instead of ~1/sec.
    Submitted in small chunks so Stop stays responsive.

    fleet=(min_pu, max_pu) is the scan-time Fleet Size filter from the
    Dashboard scan form: a carrier only qualifies when its power units
    sit inside the range (None = no limit on that side).
    """
    stop = RUNNING_SCANS[user_id]["stop"]
    started = time.time()
    try:
        mcs = list(range(start_mc, end_mc + 1))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=SCAN_WORKERS) as ex:
            for chunk_start in range(0, len(mcs), 10):
                if stop.is_set():
                    break
                # A demo trial can end while a scan is running — stop the
                # run instead of letting it keep checking for free.
                if _demo_expired_for(user_id):
                    stop.set()
                    break
                chunk = mcs[chunk_start:chunk_start + 10]
                futs = {ex.submit(_safe_check_mc, mc, cargo_types): mc
                        for mc in chunk}
                for fut in concurrent.futures.as_completed(futs):
                    if stop.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    rec = fut.result()
                    if rec.get("qualified"):
                        # Scan-time Fleet Size filter: an otherwise
                        # qualified carrier outside the set power-units
                        # range is skipped, not qualified.
                        min_pu, max_pu = fleet
                        if not _fleet_in_range(rec.get("power_units"),
                                               min_pu, max_pu):
                            if min_pu is not None and max_pu is not None:
                                rng = "%d–%d" % (min_pu, max_pu)
                            elif min_pu is not None:
                                rng = "at least %d" % min_pu
                            else:
                                rng = "at most %d" % max_pu
                            rec["qualified"] = False
                            rec["status"] = "Skip — Fleet size"
                            rec["note"] = ("Fleet size %s power units is"
                                           " outside the %s range"
                                           % (rec.get("power_units")
                                              or "unknown", rng))
                    try:
                        _write_scan_result(user_id, scan_db_id, scan_id, rec)
                    except Exception:
                        # Audit fix 5: one failed write used to kill the
                        # whole scan silently (and the scan still showed
                        # "Completed"). Log it, count the MC as an error,
                        # and keep checking the rest.
                        app.logger.exception(
                            "Scan %s: failed to write result for MC %s",
                            scan_id, rec.get("mc_number"))
                        _count_scan_write_error(user_id, scan_db_id, scan_id,
                                                rec)
                if stop.is_set():
                    break
    finally:
        duration = int(time.time() - started)
        status = "Stopped" if stop.is_set() else "Completed"
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE scans SET status=?, finished_at=?, duration_seconds=?"
                " WHERE id=?", (status, now_str(), duration, scan_db_id))
            conn.commit()
        finally:
            conn.close()
        RUNNING_SCANS.pop(user_id, None)


def _write_motus_result(user_id, run_db_id, kind, usdot, rec):
    """Persist one finished Motus/Register check (same pattern as scans)."""
    source = "motus" if kind == "motus" else "register"
    with _DB_WRITE_LOCK:
        conn = store.connect()
        try:
            _log_check(conn, user_id, "motus-%s" % run_db_id, rec)
            if rec["qualified"]:
                conn.execute(
                    "INSERT INTO carriers (user_id, scan_id, source, mc_number,"
                    " usdot_number, legal_name, dba_name, entity_type,"
                    " authority_status, cargo_carried, cargo_categories,"
                    " address_line1, city, state, phone, power_units, checked_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, "motus-%s" % run_db_id, source,
                     rec["mc_number"], rec.get("usdot_number") or usdot,
                     rec.get("legal_name") or "", rec.get("dba_name") or "",
                     rec.get("entity_type") or "",
                     rec.get("authority_status") or "",
                     json.dumps(rec.get("cargo_carried") or []),
                     json.dumps(rec.get("cargo_categories") or []),
                     rec.get("address_line1") or "", rec.get("city") or "",
                     rec.get("state") or "", rec.get("phone") or "",
                     rec.get("power_units") or "", now_str()))
            if rec["status"] == "Error":
                conn.execute(
                    "INSERT INTO error_mcs (user_id, scan_id, source, mc_number,"
                    " error, checked_at) VALUES (?,?,?,?,?,?)",
                    (user_id, "motus-%s" % run_db_id, source,
                     rec["mc_number"], rec.get("note") or "Fetch error",
                     now_str()))
            conn.execute(
                "UPDATE motus_runs SET checked=checked+1,"
                " qualified=qualified+?, errors=errors+? WHERE id=?",
                (1 if rec["qualified"] else 0,
                 1 if rec["status"] == "Error" else 0, run_db_id))
            conn.commit()
        finally:
            conn.close()


def run_motus(user_id, run_db_id, kind, usdots, cargo_types):
    """Background thread: check a pasted list of USDOTs against SAFER.

    Same parallel + proxy engine as run_scan, in small chunks.
    """
    stop = RUNNING_MOTUS[user_id]["stop"]
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=SCAN_WORKERS) as ex:
            for chunk_start in range(0, len(usdots), 10):
                if stop.is_set():
                    break
                # A demo trial can end while a run is going — stop the run
                # instead of letting it keep checking for free.
                if _demo_expired_for(user_id):
                    stop.set()
                    break
                chunk = usdots[chunk_start:chunk_start + 10]
                futs = {ex.submit(_safe_check_usdot, u, cargo_types): u
                        for u in chunk}
                for fut in concurrent.futures.as_completed(futs):
                    if stop.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    _write_motus_result(user_id, run_db_id, kind,
                                        futs[fut], fut.result())
                if stop.is_set():
                    break
    finally:
        conn = store.connect()
        try:
            conn.execute(
                "UPDATE motus_runs SET status=?, finished_at=? WHERE id=?",
                ("Stopped" if stop.is_set() else "Completed", now_str(), run_db_id))
            conn.commit()
        finally:
            conn.close()
        RUNNING_MOTUS.pop(user_id, None)


# ---------------------------------------------------------------- auth pages

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ---------------------------------------------------------------- public search

def _digits(s):
    return re.sub(r"\D", "", s or "")


def _safer_url(param, value):
    """Link to verify one record directly on the FMCSA SAFER website."""
    return ("https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY"
            "&query_type=queryCarrierSnapshot&query_param=%s"
            "&query_string=%s" % (param, value))


def _detail_result(rec, param):
    """Wrap a classified SAFER record for the search page."""
    value = rec.get("mc_number") if param == "MC_MX" else (
        rec.get("usdot_number") or rec.get("mc_number"))
    dot = rec.get("usdot_number") or ""
    if dot and not rec.get("owner_officer"):
        rec["owner_officer"] = fetch_company_officer(dot)
    # Cargo-based estimate of the carrier's truck types (SAFER records
    # no truck type) — shown in the "Expected Truck" box on the detail.
    rec["expected_trucks"] = expected_trucks(rec.get("cargo_carried"))
    return {"kind": "detail", "rec": rec,
            "safer_url": _safer_url(param, value or "")}


def _local_carriers_by_phone(digits):
    """Saved carriers whose phone contains these digits (any format)."""
    if len(digits) < 7:
        return []
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM carriers WHERE phone IS NOT NULL AND phone != ''"
            " ORDER BY checked_at DESC LIMIT 500").fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for row in rows:
        d = dict(row)
        if digits in _digits(d.get("phone")) and d["mc_number"] not in seen:
            seen.add(d["mc_number"])
            out.append(carrier_dict(d))
    return out


def _local_carriers_by_name(q):
    """Saved carriers whose legal or DBA name contains q."""
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM carriers WHERE legal_name LIKE ? OR dba_name LIKE ?"
            " ORDER BY checked_at DESC LIMIT 50",
            ("%" + q + "%", "%" + q + "%")).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for row in rows:
        d = dict(row)
        if d["mc_number"] not in seen:
            seen.add(d["mc_number"])
            out.append(carrier_dict(d))
    return out


def _name_search(q):
    """Company-name search: live SAFER keyword matches first, saved
    carriers as a fallback when SAFER finds nobody (or is unreachable)."""
    matches, err = search_by_name(q, direct_first=True)
    if err:
        app.logger.exception("SAFER name search failed for %r", q)
        rows = _local_carriers_by_name(q)
        if rows:
            return {"kind": "local_matches", "rows": rows,
                    "heading": "Saved carriers matching this name",
                    "note": "Live SAFER name search is unavailable right now — showing carriers we already checked."}
        return {"kind": "message",
                "text": "SAFER name search is unavailable right now. Try an MC or USDOT number instead."}
    if not matches:
        rows = _local_carriers_by_name(q)
        if rows:
            return {"kind": "local_matches", "rows": rows,
                    "heading": "No live SAFER match — saved carriers matching this name"}
        return {"kind": "message",
                "text": "No carrier named \u201c%s\u201d found on SAFER, and none in our checked carriers either. Check the spelling or search by MC / USDOT." % q}
    if len(matches) == 1:
        rec = check_usdot(matches[0]["usdot"], ("general", "fresh", "reefer"),
                          direct_first=True)
        res = _detail_result(rec, "USDOT")
        res["matched"] = matches[0]
        return res
    return {"kind": "name_matches", "matches": matches,
            "total": len(matches)}


def _run_carrier_search(q, stype):
    """Run a public carrier search. Result kinds: detail | ambiguous |
    name_matches | local_matches | message."""
    if stype == "mc":
        return _detail_result(check_mc(q, ("general", "fresh", "reefer"),
                                       direct_first=True), "MC_MX")
    if stype == "usdot":
        return _detail_result(check_usdot(q, ("general", "fresh", "reefer"),
                                          direct_first=True), "USDOT")
    if stype == "phone":
        digits = _digits(q)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        rows = _local_carriers_by_phone(digits)
        if rows:
            return {"kind": "local_matches", "rows": rows,
                    "heading": "Saved carriers with this phone number"}
        return {"kind": "message",
                "text": "No carrier with this phone number in our checked carriers yet \u2014 search by MC or USDOT for a live SAFER check."}
    if stype == "email":
        # The app stores no carrier email addresses, so there is nothing
        # to match an email against.
        return {"kind": "message",
                "text": "We don't store carrier email addresses, so there's nothing to match an email against \u2014 search by MC, USDOT or company name instead."}
    if stype == "name":
        return _name_search(q)
    # auto-detect
    if "@" in q:
        return _run_carrier_search(q, "email")
    stripped = q.strip()
    digits = _digits(q)
    if stripped.isdigit() and len(stripped) <= 8:
        # Could be a USDOT or an MC — check both, like CarrierChk does.
        # A record only counts as a real alternative when SAFER returns a
        # usable carrier (an inactive record has no name — it is not a
        # second carrier, just a stale number).
        dot = check_usdot(stripped, ("general", "fresh", "reefer"),
                          direct_first=True)
        mc = check_mc(stripped, ("general", "fresh", "reefer"),
                      direct_first=True)

        def _usable(rec):
            return bool(rec.get("found")) and not rec.get("fetch_error") \
                and bool(rec.get("legal_name"))

        dot_usable, mc_usable = _usable(dot), _usable(mc)
        if dot_usable and mc_usable:
            return {"kind": "ambiguous", "number": stripped,
                    "dot": _detail_result(dot, "USDOT"),
                    "mc": _detail_result(mc, "MC_MX")}
        if dot_usable:
            return _detail_result(dot, "USDOT")
        if mc_usable:
            return _detail_result(mc, "MC_MX")
        # Neither is a live carrier: show whichever record exists at all
        # (e.g. an inactive one), MC first, else the DOT record.
        if mc.get("found") and not mc.get("fetch_error"):
            return _detail_result(mc, "MC_MX")
        return _detail_result(dot, "USDOT")
    if digits and (len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))):
        return _run_carrier_search(q, "phone")
    if stripped.isdigit():
        return _run_carrier_search(stripped, "mc")
    return _name_search(q)


BULK_MAX_LINES = 50


def _bulk_row(input_text, kind, rec):
    return {"input": input_text, "kind": kind,
            "mc_number": rec.get("mc_number") or "",
            "usdot_number": rec.get("usdot_number") or "",
            "legal_name": rec.get("legal_name") or "",
            "city": rec.get("city") or "", "state": rec.get("state") or "",
            "phone": rec.get("phone") or "",
            "status": rec.get("status") or "",
            "authority_status": rec.get("authority_status") or "",
            "cargo": ", ".join(rec.get("cargo_carried") or []),
            "qualified": bool(rec.get("qualified")),
            "note": rec.get("note") or ""}


def _bulk_line_check(line):
    """Check one bulk line. Returns row dicts (usually one; two when the
    number is both a live USDOT and a live MC)."""
    s = line.strip()
    m = re.match(r"^(mc|dot|usdot)[\s\-#]*(\d+)\s*$", s, re.I)
    if m:
        kind, num = m.group(1).lower(), m.group(2)
        if kind == "mc":
            return [_bulk_row(s, "MC",
                              check_mc(num, ("general", "fresh", "reefer")))]
        return [_bulk_row(s, "USDOT",
                          check_usdot(num, ("general", "fresh", "reefer")))]
    digits = _digits(s)
    if not digits or not digits.isdigit() or len(digits) > 8:
        return [{"input": s, "kind": "—", "mc_number": "",
                 "usdot_number": "", "legal_name": "", "city": "",
                 "state": "", "phone": "", "status": "Error",
                 "authority_status": "", "cargo": "", "qualified": False,
                 "note": "Not a valid MC or DOT number — one number per line."}]
    dot = check_usdot(digits, ("general", "fresh", "reefer"))
    mc = check_mc(digits, ("general", "fresh", "reefer"))

    def _usable(rec):
        return bool(rec.get("found")) and not rec.get("fetch_error") \
            and bool(rec.get("legal_name"))

    rows = []
    if _usable(dot):
        rows.append(_bulk_row(s, "USDOT", dot))
    if _usable(mc):
        rows.append(_bulk_row(s, "MC", mc))
    if rows:
        return rows
    if dot.get("found") and not dot.get("fetch_error"):
        return [_bulk_row(s, "USDOT", dot)]
    return [_bulk_row(s, "MC", mc)]


def _run_bulk_search(raw):
    """Bulk search: one MC/DOT per line, up to BULK_MAX_LINES, checked
    in parallel. Returns the bulk result dict."""
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    seen, uniq = set(), []
    for ln in lines:
        key = ln.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(ln)
    capped = len(uniq) > BULK_MAX_LINES
    uniq = uniq[:BULK_MAX_LINES]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=SCAN_WORKERS) as ex:
        futs = [ex.submit(_bulk_line_check, ln) for ln in uniq]
        for fut in futs:  # in input order
            try:
                rows.extend(fut.result())
            except Exception as exc:
                rows.append({"input": "", "kind": "—", "mc_number": "",
                             "usdot_number": "", "legal_name": "",
                             "city": "", "state": "", "phone": "",
                             "status": "Error", "authority_status": "",
                             "cargo": "", "qualified": False,
                             "note": "Check failed: %s" % exc})
    return {"kind": "bulk", "rows": rows, "total": len(uniq),
            "qualified": sum(1 for r in rows if r["qualified"]),
            "not_found": sum(1 for r in rows if r["status"] == "Not Found"),
            "errors": sum(1 for r in rows if r["status"] == "Error"),
            "capped": capped, "cap": BULK_MAX_LINES}


@app.route("/search", methods=["GET", "POST"])
def carrier_search():
    """Public carrier search — no login needed. MC, USDOT, phone, email
    or company name; live SAFER data for numbers and names, saved
    carriers for phone/email reverse lookup. Bulk mode checks a pasted
    list of MC/DOT numbers (up to 50) in one go."""
    user = current_user()
    # Single Search is the only single-search mode (its type dropdown
    # defaults to Auto-detect, so a separate Auto mode is redundant).
    # An old ?mode=auto link/bookmark is normalized to single, never an
    # error — the single form with Auto-detect behaves the same way.
    mode = (request.values.get("mode") or "single").strip().lower()
    if mode not in ("single", "bulk"):
        mode = "single"
    q = (request.values.get("q") or "").strip()
    # An explicit type (from the disambiguation "View record" links or the
    # Single Search form) always wins — even when mode is absent and
    # defaults to "single". Only fall back to auto-detect when no type was
    # given at all. (Previously `mode == "auto"` forced stype back to
    # "auto", so the disambiguation links and Single Search with a chosen
    # type both re-ran auto-detect and showed the disambiguation again.)
    type_param = request.values.get("type")
    stype = (type_param or "auto").strip().lower()
    if stype not in ("auto", "mc", "usdot", "phone", "email", "name"):
        stype = "auto"
    bulk = (request.values.get("bulk") or "").strip()
    result = None
    # H3: rate-limit the public search so one IP cannot hammer SAFER /
    # Socrata or exhaust the server. Limits are generous for real use.
    if mode == "bulk" and bulk:
        bulk_limit = 30 if user else 10  # per hour
        if _rate_limited(("bulk", request.remote_addr), bulk_limit, 3600):
            result = {"kind": "message",
                      "text": "Too many bulk searches from this address — please try again later."}
            return render_template("search.html", user=user, q=q, stype=stype,
                                   result=result, mode=mode, bulk=bulk), 429
        try:
            result = _run_bulk_search(bulk)
        except Exception:
            app.logger.exception("Bulk search failed")
            result = {"kind": "message",
                      "text": "Bulk search failed. Please try again."}
    elif q:
        if _rate_limited(("search", request.remote_addr), 30, 60):
            result = {"kind": "message",
                      "text": "Too many searches from this address — please wait a minute and try again."}
            return render_template("search.html", user=user, q=q, stype=stype,
                                   result=result, mode=mode, bulk=bulk), 429
        try:
            result = _run_carrier_search(q, stype)
        except Exception:
            app.logger.exception("Carrier search failed for %r", q)
            result = {"kind": "message",
                      "text": "Search failed. Please try again."}
    return render_template("search.html", user=user, q=q, stype=stype,
                           result=result, mode=mode, bulk=bulk)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        # H1: login attempts are rate-limited per IP (5/min, 20/hour).
        if _rate_limited(("login", request.remote_addr), 5, 60) or \
                _rate_limited(("login-h", request.remote_addr), 20, 3600):
            error = "Too many login attempts from this address. Try again later."
            return render_template("login.html", error=error), 429
        form = request.form.get("form", "login")
        conn = store.connect()
        try:
            if form == "signup":
                email = (request.form.get("email") or "").strip().lower()[:254]
                username = (request.form.get("username") or "").strip()[:50]
                password = request.form.get("password") or ""
                if not email or not username or len(password) < MIN_PASSWORD_LEN:
                    error = "Email, username and a password of %d+ characters are required." % MIN_PASSWORD_LEN
                elif len(password) > 128:
                    error = "Password must be at most 128 characters."
                elif conn.execute("SELECT email FROM closed_demos WHERE email=?",
                                  (email,)).fetchone():
                    # A Demo account that was auto-closed can never sign
                    # up again with the same email — it must pay $5/month
                    # (owner's call) instead of getting a fresh trial.
                    # Admin manual creation bypasses this (separate route).
                    error = DEMO_BLOCKED_MESSAGE
                elif conn.execute("SELECT id FROM users WHERE email=? AND blocked=1",
                                  (email,)).fetchone():
                    # Owner block: a blocked email can never sign up
                    # again — no code is sent. Separate from the
                    # closed-Demo blocklist above; the messages differ.
                    error = BLOCKED_MESSAGE
                elif conn.execute("SELECT id FROM users WHERE email=? OR username=?",
                                  (email, username)).fetchone():
                    error = "An account with this email or username already exists. Try logging in instead."
                else:
                    # Signup email verification (Zack 2026-11-21): the
                    # account is created ONLY after the user proves
                    # they own the email with a 6-digit code. No user
                    # row exists until then — the pending signup
                    # (username + hashed password) lives server-side
                    # in pending_signups, and the final creation step
                    # reads it from there, never from client data.
                    # C1: signup NEVER creates an admin. The admin
                    # account comes only from ROAD_ADMIN_EMAIL /
                    # ROAD_ADMIN_PASSWORD. A new signup is a 7-day
                    # demo trial (plan 'Demo'); only the admin can
                    # give Lifetime access.
                    if _rate_limited(("signup-verify-request", email),
                                     OTP_MAX_REQUESTS, OTP_REQUEST_WINDOW):
                        error = ("Too many code requests for this email. "
                                 "Try again later.")
                        return render_template("login.html", error=error), 429
                    code = "%06d" % secrets.randbelow(1000000)
                    expires = (datetime.now()
                               + timedelta(minutes=OTP_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("UPDATE pending_signups SET used=1"
                                 " WHERE email=? AND used=0", (email,))
                    conn.execute(
                        "INSERT INTO pending_signups (email, username,"
                        " password_hash, remember, code_hash, used,"
                        " created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                        (email, username, generate_password_hash(password),
                         1 if request.form.get("remember") else 0,
                         generate_password_hash(code), 0, now_str(), expires))
                    conn.commit()
                    _otp_fails_clear(email)  # a fresh code restarts the guess budget
                    ok, send_error = _send_signup_verify_email(email, code)
                    if not ok:
                        # The pending row is already stored; without a
                        # sent email its code can never be used (it
                        # expires in 10 minutes) — but drop it anyway
                        # so a later misconfiguration cannot leak a
                        # live code.
                        conn.execute("UPDATE pending_signups SET used=1"
                                     " WHERE email=? AND used=0", (email,))
                        conn.commit()
                        error = send_error
                    else:
                        return render_template(
                            "login.html", error="",
                            message=("We emailed a 6-digit verification code "
                                     "to %s. Enter it below to finish creating "
                                     "your account. It expires in %d minutes."
                                     % (email, OTP_EXPIRY_MINUTES)),
                            signup_verify=True, signup_email=email)
            else:
                ident = (request.form.get("identifier") or "").strip()
                password = request.form.get("password") or ""
                # A half-finished 2FA step from an earlier attempt must
                # not linger into this login.
                session.pop("totp_user_id", None)
                session.pop("totp_remember", None)
                user = conn.execute(
                    "SELECT * FROM users WHERE email=? OR username=?",
                    (ident.lower(), ident)).fetchone()
                ok = bool(user and check_password_hash(user["password_hash"], password))
                if ok and _is_blocked(user):
                    # Owner block: refused at login, even with the right
                    # password. Checked only after the password checks
                    # out — like the expired-Demo close below — so a
                    # wrong password never reveals the block. No
                    # session is created and nothing is deleted.
                    conn.execute(
                        "INSERT INTO login_events (user_id, identifier,"
                        " success, ip, created_at) VALUES (?,?,?,?,?)",
                        (user["id"], ident, 0, request.remote_addr, now_str()))
                    conn.commit()
                    error = BLOCKED_MESSAGE
                    return render_template("login.html", error=error)
                if ok:
                    # Auto-close: a Demo trial that has ended closes the
                    # account on login — but only after the password
                    # checks out, so a wrong-password attempt deletes
                    # nothing. 2FA never applies here (only the admin
                    # can enable 2FA, and the admin never expires).
                    expired, _ = _demo_status(dict(user))
                    if expired:
                        _close_expired_demo(dict(user))
                        error = _expiry_closed_message(dict(user))
                        return render_template("login.html", error=error)
                if ok and _totp_enabled(user):
                    # Password is right, but 2FA is on for this account:
                    # hold the login at the second step. No login event
                    # or last-login update yet — those happen in
                    # login_totp once the code checks out.
                    if pyotp is None:
                        error = ("Two-factor verification is unavailable"
                                 " right now. Try again later.")
                    else:
                        session["totp_user_id"] = user["id"]
                        session["totp_remember"] = bool(request.form.get("remember"))
                        return render_template("login.html", error="",
                                               totp_required=True,
                                               totp_username=user["username"])
                    return render_template("login.html", error=error)
                conn.execute(
                    "INSERT INTO login_events (user_id, identifier, success, ip,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (user["id"] if user else None, ident, 1 if ok else 0,
                     request.remote_addr, now_str()))
                if ok:
                    conn.execute(
                        "UPDATE users SET last_login=?, login_count=login_count+1"
                        " WHERE id=?", (now_str(), user["id"]))
                    conn.commit()
                    _login_user(user, request.form.get("remember"))
                    return redirect(url_for("dashboard"))
                conn.commit()
                error = "Invalid username/email or password."
        finally:
            conn.close()
    return render_template("login.html", error=error)


@app.route("/login/totp", methods=["POST"])
def login_totp():
    """Second login step for accounts with 2FA enabled.

    Reached only after the password step succeeded (session holds
    totp_user_id). Wrong codes share the login rate-limit budget
    (5/min, 20/hour per IP) and are logged as failed logins, exactly
    like wrong passwords."""
    if _rate_limited(("login", request.remote_addr), 5, 60) or \
            _rate_limited(("login-h", request.remote_addr), 20, 3600):
        error = "Too many login attempts from this address. Try again later."
        return render_template("login.html", error=error,
                               totp_required=True), 429
    uid = session.get("totp_user_id")
    if not uid:
        return redirect(url_for("login"))
    conn = store.connect()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user or not _totp_enabled(user):
            session.pop("totp_user_id", None)
            session.pop("totp_remember", None)
            return redirect(url_for("login"))
        code = request.form.get("totp_code") or ""
        if pyotp is None or not _totp_verify(user["totp_secret"], code):
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success, ip,"
                " created_at) VALUES (?,?,?,?,?)",
                (user["id"], user["username"], 0,
                 request.remote_addr, now_str()))
            conn.commit()
            return render_template("login.html",
                                   error="Invalid verification code. Try again.",
                                   totp_required=True,
                                   totp_username=user["username"])
        conn.execute(
            "INSERT INTO login_events (user_id, identifier, success, ip,"
            " created_at) VALUES (?,?,?,?,?)",
            (user["id"], user["username"], 1,
             request.remote_addr, now_str()))
        conn.execute(
            "UPDATE users SET last_login=?, login_count=login_count+1"
            " WHERE id=?", (now_str(), user["id"]))
        conn.commit()
        remember = session.pop("totp_remember", None)
        session.pop("totp_user_id", None)
        _login_user(user, remember)
        return redirect(url_for("dashboard"))
    finally:
        conn.close()


@app.route("/login/otp", methods=["GET"])
def login_otp():
    """Email-code sign-in, step 1: ask for the email/username."""
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("login.html", error="", otp_request=True)


@app.route("/login/otp/request", methods=["POST"])
def login_otp_request():
    """Email-code sign-in, step 1 submit: email the 6-digit code.

    The reply never reveals whether the identifier has an account
    (same as /forgot) — but a code is only sent when it does. An
    expired Demo account is NOT closed here: the code is the
    credential, and closing happens only after it checks out, in
    login_otp_verify (like the password login)."""
    if current_user():
        return redirect(url_for("dashboard"))
    # A half-finished 2FA step from an earlier attempt must not linger.
    session.pop("totp_user_id", None)
    session.pop("totp_remember", None)
    ident = (request.form.get("identifier") or "").strip()
    email_key = ident.lower()
    # 5 OTP requests per email per 15 minutes.
    if _rate_limited(("otp-request", email_key), OTP_MAX_REQUESTS,
                     OTP_REQUEST_WINDOW):
        return render_template("login.html", error="Too many code requests "
                               "for this email. Try again later.",
                               otp_request=True), 429
    message = ("If an account exists for that email/username, a 6-digit "
               "login code was sent to the registered email. It expires "
               "in %d minutes." % OTP_EXPIRY_MINUTES)
    conn = store.connect()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email=? OR username=?",
            (email_key, ident)).fetchone()
        if user and _is_blocked(user):
            # Owner block: never send a login code to a blocked user.
            # The reply stays generic (same as an unknown identifier)
            # so the block is not revealed to anyone typing the email.
            return render_template("login.html", error="", message=message,
                                   otp_verify=True, otp_identifier=ident)
        if not user:
            return render_template("login.html", error="", message=message,
                                   otp_verify=True, otp_identifier=ident)
        code = "%06d" % secrets.randbelow(1000000)
        expires = (datetime.now()
                   + timedelta(minutes=OTP_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE login_otps SET used=1 WHERE user_id=? AND used=0",
                     (user["id"],))
        conn.execute(
            "INSERT INTO login_otps (user_id, code_hash, used, created_at,"
            " expires_at) VALUES (?,?,?,?,?)",
            (user["id"], generate_password_hash(code), 0, now_str(), expires))
        conn.commit()
        _otp_fails_clear(email_key)  # a fresh code restarts the guess budget
    finally:
        conn.close()
    ok, send_error = _send_login_otp_email(user["email"], code)
    if not ok:
        # The code row is already stored; without a sent email it can
        # never be used (it expires in 10 minutes) — but drop it
        # anyway so a later misconfiguration cannot leak a live code.
        conn = store.connect()
        try:
            conn.execute("UPDATE login_otps SET used=1 WHERE user_id=? AND used=0",
                         (user["id"],))
            conn.commit()
        finally:
            conn.close()
        return render_template("login.html", error=send_error,
                               otp_request=True)
    _log_admin_action(None, user["id"], "otp_code_sent")
    return render_template("login.html", error="", message=message,
                           otp_verify=True, otp_identifier=ident)


@app.route("/login/otp/verify", methods=["POST"])
def login_otp_verify():
    """Email-code sign-in, step 2: check the 6-digit code and log in.

    Wrong codes share the login rate-limit budget (like login_totp)
    and the per-email guess budget (5 per 15 min); after 5 wrong
    guesses the live code is cancelled. A correct code is single-use.
    After the code checks out, an expired Demo account is closed
    (never logged in) and a TOTP-enabled account still goes through
    the unchanged TOTP step."""
    if current_user():
        return redirect(url_for("dashboard"))
    if _rate_limited(("login", request.remote_addr), 5, 60) or \
            _rate_limited(("login-h", request.remote_addr), 20, 3600):
        error = "Too many login attempts from this address. Try again later."
        return render_template("login.html", error=error,
                               otp_verify=True,
                               otp_identifier=request.form.get("identifier") or ""), 429
    ident = (request.form.get("identifier") or "").strip()
    code = (request.form.get("code") or "").strip()
    email_key = ident.lower()
    if _rate_limited(("otp-verify", email_key), OTP_MAX_ATTEMPTS,
                     OTP_VERIFY_WINDOW):
        error = "Too many code attempts for this email. Try again later."
        return render_template("login.html", error=error,
                               otp_verify=True, otp_identifier=ident), 429
    conn = store.connect()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email=? OR username=?",
            (email_key, ident)).fetchone()
        row = None
        if user:
            row = conn.execute(
                "SELECT * FROM login_otps WHERE user_id=? AND used=0"
                " ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not user or not row:
            fails = _otp_fails_inc(email_key)
            cancelled = False
            if user and fails >= OTP_MAX_ATTEMPTS:
                conn.execute("UPDATE login_otps SET used=1"
                             " WHERE user_id=? AND used=0", (user["id"],))
                _otp_fails_clear(email_key)
                cancelled = True
            if user:
                conn.execute(
                    "INSERT INTO login_events (user_id, identifier, success,"
                    " ip, created_at) VALUES (?,?,?,?,?)",
                    (user["id"], ident, 0, request.remote_addr, now_str()))
            conn.commit()
            error = ("Too many wrong codes — this code was cancelled. "
                     "Request a new one.") if cancelled else \
                    "Invalid or expired code. Request a new one."
            return render_template("login.html", error=error,
                                   otp_verify=True, otp_identifier=ident)
        if row["expires_at"] < now_str():
            _otp_fails_clear(email_key)
            return render_template("login.html",
                                   error="That code has expired. Request a new one.",
                                   otp_verify=True, otp_identifier=ident)
        if not check_password_hash(row["code_hash"], code):
            fails = _otp_fails_inc(email_key)
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success,"
                " ip, created_at) VALUES (?,?,?,?,?)",
                (user["id"], ident, 0, request.remote_addr, now_str()))
            if fails >= OTP_MAX_ATTEMPTS:
                conn.execute("UPDATE login_otps SET used=1"
                             " WHERE user_id=? AND used=0", (user["id"],))
                conn.commit()
                _otp_fails_clear(email_key)
                error = ("Too many wrong codes — this code was cancelled. "
                         "Request a new one.")
            else:
                conn.commit()
                error = "Invalid code. Try again."
            return render_template("login.html", error=error,
                                   otp_verify=True, otp_identifier=ident)
        # Code is right: single-use, then the normal login steps.
        conn.execute("UPDATE login_otps SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        _otp_fails_clear(email_key)
        user = dict(user)
    finally:
        conn.close()
    if _is_blocked(user):
        # Owner block: a code issued before the block can never be
        # used after it — and the code is already spent (single-use).
        # Checked only after the code checks out, so a wrong code
        # never reveals the block.
        conn = store.connect()
        try:
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success,"
                " ip, created_at) VALUES (?,?,?,?,?)",
                (user["id"], ident, 0, request.remote_addr, now_str()))
            conn.commit()
        finally:
            conn.close()
        return render_template("login.html", error=BLOCKED_MESSAGE)
    expired, _ = _demo_status(user)
    if expired:
        _close_expired_demo(user)
        return render_template("login.html",
                               error=_expiry_closed_message(user))
    if _totp_enabled(user):
        if pyotp is None:
            return render_template("login.html",
                                   error="Two-factor verification is unavailable"
                                         " right now. Try again later.",
                                   otp_verify=True, otp_identifier=ident)
        session["totp_user_id"] = user["id"]
        session["totp_remember"] = False
        return render_template("login.html", error="",
                               totp_required=True,
                               totp_username=user["username"])
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO login_events (user_id, identifier, success, ip,"
            " created_at) VALUES (?,?,?,?,?)",
            (user["id"], ident, 1, request.remote_addr, now_str()))
        conn.execute(
            "UPDATE users SET last_login=?, login_count=login_count+1"
            " WHERE id=?", (now_str(), user["id"]))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(None, user["id"], "otp_login")
    _login_user(user, False)
    return redirect(url_for("dashboard"))


@app.route("/signup/verify", methods=["POST"])
def signup_verify():
    """Signup email verification, step 2: check the 6-digit code and
    create the account.

    The pending signup (username + hashed password) is read from the
    server-side pending_signups row — never from client data. Wrong
    codes share the login rate-limit budget (like login_otp_verify)
    and the per-email guess budget (5 per 15 min); after 5 wrong
    guesses the live code is cancelled. A correct code is single-use.
    The account is created as a 7-day Demo trial, exactly as signup
    did before verification existed."""
    if current_user():
        return redirect(url_for("dashboard"))
    if _rate_limited(("login", request.remote_addr), 5, 60) or \
            _rate_limited(("login-h", request.remote_addr), 20, 3600):
        error = "Too many login attempts from this address. Try again later."
        return render_template("login.html", error=error,
                               signup_verify=True,
                               signup_email=request.form.get("email") or ""), 429
    # A half-finished 2FA step from an earlier attempt must not linger.
    session.pop("totp_user_id", None)
    session.pop("totp_remember", None)
    email = (request.form.get("email") or "").strip().lower()[:254]
    code = (request.form.get("code") or "").strip()
    if _rate_limited(("otp-verify", email), OTP_MAX_ATTEMPTS,
                     OTP_VERIFY_WINDOW):
        error = "Too many code attempts for this email. Try again later."
        return render_template("login.html", error=error,
                               signup_verify=True, signup_email=email), 429
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM pending_signups WHERE email=? AND used=0"
            " ORDER BY id DESC LIMIT 1", (email,)).fetchone()
        if not row:
            fails = _otp_fails_inc(email)
            cancelled = False
            if fails >= OTP_MAX_ATTEMPTS:
                conn.execute("UPDATE pending_signups SET used=1"
                             " WHERE email=? AND used=0", (email,))
                _otp_fails_clear(email)
                cancelled = True
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success,"
                " ip, created_at) VALUES (?,?,?,?,?)",
                (None, email, 0, request.remote_addr, now_str()))
            conn.commit()
            error = ("Too many wrong codes — this code was cancelled. "
                     "Request a new one.") if cancelled else \
                    "Invalid or expired code. Request a new one."
            return render_template("login.html", error=error,
                                   signup_verify=True, signup_email=email)
        if row["expires_at"] < now_str():
            _otp_fails_clear(email)
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success,"
                " ip, created_at) VALUES (?,?,?,?,?)",
                (None, email, 0, request.remote_addr, now_str()))
            conn.commit()
            return render_template("login.html",
                                   error="That code has expired. Request a new one.",
                                   signup_verify=True, signup_email=email)
        if not check_password_hash(row["code_hash"], code):
            fails = _otp_fails_inc(email)
            conn.execute(
                "INSERT INTO login_events (user_id, identifier, success,"
                " ip, created_at) VALUES (?,?,?,?,?)",
                (None, email, 0, request.remote_addr, now_str()))
            if fails >= OTP_MAX_ATTEMPTS:
                conn.execute("UPDATE pending_signups SET used=1"
                             " WHERE email=? AND used=0", (email,))
                conn.commit()
                _otp_fails_clear(email)
                error = ("Too many wrong codes — this code was cancelled. "
                         "Request a new one.")
            else:
                conn.commit()
                error = "Invalid code. Try again."
            return render_template("login.html", error=error,
                                   signup_verify=True, signup_email=email)
        # Code is right: single-use, then create the account from the
        # server-side pending row.
        conn.execute("UPDATE pending_signups SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        _otp_fails_clear(email)
        pending = dict(row)
    finally:
        conn.close()
    # Re-check the guards at creation time (a parallel signup or an
    # admin action could have changed things since the code was sent).
    conn = store.connect()
    try:
        if conn.execute("SELECT email FROM closed_demos WHERE email=?",
                        (pending["email"],)).fetchone():
            return render_template("login.html", error=DEMO_BLOCKED_MESSAGE)
        if conn.execute("SELECT id FROM users WHERE email=? AND blocked=1",
                        (pending["email"],)).fetchone():
            # Owner block: the email's account was blocked after the
            # code was sent — no second account is created.
            return render_template("login.html", error=BLOCKED_MESSAGE)
        if conn.execute("SELECT id FROM users WHERE email=? OR username=?",
                        (pending["email"], pending["username"])).fetchone():
            return render_template(
                "login.html",
                error="An account with this email or username already exists. Try logging in instead.")
        # Audit fix 4 (same race as the old signup INSERT): the
        # existence check above and this INSERT can race on a
        # double-click — catch the UNIQUE violation instead of 500'ing.
        try:
            conn.execute(
                "INSERT INTO users (email, username, password_hash, is_admin,"
                " plan, created_at, access_expires_at) VALUES (?,?,?,?,?,?,?)",
                (pending["email"], pending["username"],
                 pending["password_hash"], 0, "Demo", now_str(),
                 _future_str(DEMO_TRIAL_DAYS)))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return render_template(
                "login.html",
                error="An account with this email or username already exists. Try logging in instead.")
        user = conn.execute(
            "SELECT * FROM users WHERE email=?", (pending["email"],)).fetchone()
        conn.execute(
            "INSERT INTO login_events (user_id, identifier, success, ip,"
            " created_at) VALUES (?,?,?,?,?)",
            (user["id"], pending["email"], 1, request.remote_addr, now_str()))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(None, user["id"], "signup_verified")
    _login_user(user, bool(pending["remember"]))
    return redirect(url_for("dashboard"))


@app.route("/signup/verify/resend", methods=["POST"])
def signup_verify_resend():
    """Send a fresh verification code for a pending signup (same
    username/password, new code). Counts against the same per-email
    request budget as the first code (5 per 15 min)."""
    if current_user():
        return redirect(url_for("dashboard"))
    session.pop("totp_user_id", None)
    session.pop("totp_remember", None)
    email = (request.form.get("email") or "").strip().lower()[:254]
    if _rate_limited(("signup-verify-request", email),
                     OTP_MAX_REQUESTS, OTP_REQUEST_WINDOW):
        return render_template(
            "login.html",
            error="Too many code requests for this email. Try again later.",
            signup_verify=True, signup_email=email), 429
    conn = store.connect()
    try:
        if conn.execute("SELECT id FROM users WHERE email=? AND blocked=1",
                        (email,)).fetchone():
            # Owner block: no fresh code for a blocked email.
            return render_template("login.html", error=BLOCKED_MESSAGE,
                                   signup_verify=True, signup_email=email)
        row = conn.execute(
            "SELECT * FROM pending_signups WHERE email=? AND used=0"
            " ORDER BY id DESC LIMIT 1", (email,)).fetchone()
        if not row:
            return render_template(
                "login.html",
                error="No pending signup for this email. Sign up again.",
                signup_verify=True, signup_email=email)
        code = "%06d" % secrets.randbelow(1000000)
        expires = (datetime.now()
                   + timedelta(minutes=OTP_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE pending_signups SET used=1"
                     " WHERE email=? AND used=0", (email,))
        conn.execute(
            "INSERT INTO pending_signups (email, username, password_hash,"
            " remember, code_hash, used, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row["email"], row["username"], row["password_hash"],
             row["remember"], generate_password_hash(code), 0,
             now_str(), expires))
        conn.commit()
        _otp_fails_clear(email)
    finally:
        conn.close()
    ok, send_error = _send_signup_verify_email(email, code)
    if not ok:
        conn = store.connect()
        try:
            conn.execute("UPDATE pending_signups SET used=1"
                         " WHERE email=? AND used=0", (email,))
            conn.commit()
        finally:
            conn.close()
        return render_template("login.html", error=send_error,
                               signup_verify=True, signup_email=email)
    return render_template(
        "login.html", error="",
        message=("A new 6-digit verification code was emailed to %s. "
                 "It expires in %d minutes." % (email, OTP_EXPIRY_MINUTES)),
        signup_verify=True, signup_email=email)


@app.route("/logout")
def logout():
    # Flask sessions are client-side (signed cookies), so clearing the
    # session alone would leave the old cookie value valid. Bump the
    # session version first so the old cookie dies for real — this signs
    # the user out on every device.
    user = current_user()
    if user:
        _bump_session_version(user["id"])
    # Audit fix 1: keep the CSRF token across logout too — a login form
    # rendered before the logout keeps working. This is safe: the token
    # is not an auth credential, and the session-version bump above has
    # already killed the old login on every device.
    csrf = session.get("_csrf")
    session.clear()
    if csrf:
        session["_csrf"] = csrf
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Request a password reset. The admin sees the one-time code in the
    Admin page and relays it to the user."""
    message = ""
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        # Audit fix 8: like /reset, limit code requests (5 per 15 min per
        # email) so the admin's Password Reset Requests list cannot be
        # flooded. Applied before the lookup — never reveal whether the
        # email has an account.
        if _rate_limited(("forgot", email), 5, 900):
            error = "Too many reset requests for this email. Try again later."
            return render_template("forgot.html", message=message, error=error), 429
        conn = store.connect()
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user:
                code = "%06d" % secrets.randbelow(1000000)
                expires = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO password_resets (user_id, code, created_at, expires_at)"
                    " VALUES (?,?,?,?)", (user["id"], code, now_str(), expires))
                conn.commit()
            # L2: the same message either way — never reveal whether the
            # email has an account.
            message = ("If an account exists for that email, a reset was requested. "
                       "Ask the site admin for your one-time code "
                       "(Admin → Password Reset Requests), then use it on the "
                       "Reset Password page. The code expires in 1 hour.")
        finally:
            conn.close()
    return render_template("forgot.html", message=message, error=error)


@app.route("/reset", methods=["GET", "POST"])
def reset_password():
    error = ""
    done = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        code = (request.form.get("code") or "").strip()
        password = request.form.get("password") or ""
        # H2: reset-code guesses are rate-limited per email (5 per 15 min).
        if _rate_limited(("reset", email), 5, 900):
            error = "Too many attempts for this email. Try again later."
            return render_template("reset.html", error=error, done=done), 429
        conn = store.connect()
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            row = None
            if user:
                row = conn.execute(
                    "SELECT * FROM password_resets WHERE user_id=? AND code=? AND used=0"
                    " ORDER BY id DESC LIMIT 1", (user["id"], code)).fetchone()
            if not user or not row:
                # H2: after 5 failed guesses the live code is invalidated,
                # so the 6-digit code cannot be brute-forced. The count
                # lives in SQLite (audit fix 9) — shared across workers.
                fails = _reset_fails_inc(email)
                if user and fails >= 5:
                    conn.execute("UPDATE password_resets SET used=1"
                                 " WHERE user_id=? AND used=0", (user["id"],))
                    conn.commit()
                    _reset_fails_clear(email)
                error = "Invalid email or code."
            elif row["expires_at"] < now_str():
                error = "That code has expired. Request a new one."
            elif len(password) < MIN_PASSWORD_LEN or len(password) > 128:
                error = "Password must be %d–128 characters." % MIN_PASSWORD_LEN
            else:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                             (generate_password_hash(password), user["id"]))
                conn.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
                conn.commit()
                _reset_fails_clear(email)
                _bump_session_version(user["id"])  # M2: kill other sessions
                _log_admin_action(None, user["id"], "password_reset")
                done = True
        finally:
            conn.close()
    return render_template("reset.html", error=error, done=done)


# ---------------------------------------------------------------- dashboard

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    settings = get_settings(user["id"])
    # A leftover scan from an earlier session must show as Paused, never
    # resume on its own.
    mark_stale_scans_paused(user["id"])
    conn = store.connect()
    try:
        scan = conn.execute(
            "SELECT * FROM scans WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user["id"],)).fetchone()
        log = conn.execute(
            "SELECT * FROM scan_log WHERE user_id=? ORDER BY id DESC LIMIT 25",
            (user["id"],)).fetchall()
        totals = conn.execute(
            "SELECT COUNT(*) c, SUM(qualified) q FROM scans WHERE user_id=?",
            (user["id"],)).fetchone()
        # CSV export has no row cap — show how many carriers the
        # Download CSV button will export, so the count is visible
        # where the download is offered.
        scan_csv_count = conn.execute(
            "SELECT COUNT(*) FROM carriers WHERE user_id=? AND source='scan'",
            (user["id"],)).fetchone()[0]
        demo_expired, demo_days_left = _demo_status(user)
        return render_template(
            "dashboard.html", user=user, settings=settings,
            scan=dict(scan) if scan else None,
            log=[dict(r) for r in log],
            total_scanned=totals["c"] or 0,
            scan_csv_count=scan_csv_count,
            commodities=commodity_names(),
            running=user["id"] in RUNNING_SCANS,
            demo_expired=demo_expired,
            demo_days_left=demo_days_left,
            access_end_date=_format_access_end(_access_expiry(user)))
    finally:
        conn.close()


@app.route("/api/status")
@login_required
def api_status():
    user = current_user()
    mark_stale_scans_paused(user["id"])
    conn = store.connect()
    try:
        scan = conn.execute(
            "SELECT * FROM scans WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user["id"],)).fetchone()
        if not scan:
            return jsonify({"running": False, "current": 0, "total": 0,
                            "results_count": 0, "error_count": 0,
                            "not_found_count": 0, "elapsed_seconds": 0,
                            "eta_seconds": 0, "rate_per_min": 0,
                            "recent_log": [], "status": "Idle",
                            "can_resume": False})
        scan = dict(scan)
        log = conn.execute(
            "SELECT * FROM scan_log WHERE user_id=? AND scan_id=? "
            "ORDER BY id DESC LIMIT 25",
            (user["id"], scan["scan_id"])).fetchall()
        elapsed = scan["duration_seconds"] or 0
        if scan["status"] == "running" and scan["started_at"]:
            # Audit fix 10: a malformed stored timestamp (only reachable
            # by direct DB edits) used to 500 every status poll.
            try:
                elapsed = int((datetime.now() - datetime.strptime(
                    scan["started_at"], "%Y-%m-%d %H:%M:%S")).total_seconds())
            except (TypeError, ValueError):
                elapsed = 0
        rate = (scan["checked"] / elapsed * 60) if elapsed > 0 else 0
        remaining = scan["total"] - scan["checked"]
        eta = int(remaining / rate * 60) if rate > 0 else 0
        return jsonify({
            "running": user["id"] in RUNNING_SCANS,
            "current": scan["checked"], "total": scan["total"],
            "results_count": scan["qualified"],
            "error_count": scan["errors"],
            "not_found_count": scan["not_found"],
            "elapsed_seconds": elapsed, "eta_seconds": eta,
            "rate_per_min": round(rate, 1),
            "start_mc": scan["start_mc"], "end_mc": scan["end_mc"],
            "status": scan["status"],
            "can_resume": scan["status"] in ("Paused", "Stopped"),
            "recent_log": [{
                "mc_number": "MC-%s" % r["mc_number"], "status": r["status"],
                "entity": r["entity"], "power_units": r["power_units"],
                "cargo": r["cargo"], "checked_at": r["checked_at"],
                "note": r["note"]} for r in log],
        })
    finally:
        conn.close()


@app.route("/api/start-scan", methods=["POST"])
@login_required
def api_start_scan():
    user = current_user()
    expired, _ = _demo_status(user)
    if expired:
        return jsonify({"ok": False,
                        "error": _expiry_message(user)}), 403
    # H5: at most 10 scan starts per user per hour.
    if _rate_limited(("scan", user["id"]), 10, 3600):
        return jsonify({"ok": False,
                        "error": "Too many scans started — try again later."}), 429
    # L5: the one-scan check lives in the database, not only in process
    # memory, so it holds across the 2 gunicorn workers.
    mark_stale_scans_paused(user["id"])
    conn = store.connect()
    try:
        running = conn.execute(
            "SELECT id FROM scans WHERE user_id=? AND status='running' LIMIT 1",
            (user["id"],)).fetchone()
    finally:
        conn.close()
    if running or user["id"] in RUNNING_SCANS:
        return jsonify({"ok": False, "error": "A scan is already running."}), 400
    body = request.get_json(silent=True) or {}
    try:
        start_mc = int(request.form.get("start_mc") or body.get("start_mc"))
        end_mc = int(request.form.get("end_mc") or body.get("end_mc"))
    except (AttributeError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Start and End MC are required."}), 400
    if end_mc < start_mc or end_mc - start_mc > 100000:
        return jsonify({"ok": False,
                        "error": "Range must be valid and at most 100,000 MCs."}), 400
    if _demo_scan_too_big(user, start_mc, end_mc):
        return jsonify({"ok": False,
                        "error": DEMO_SCAN_LIMIT_MESSAGE}), 403
    settings = get_settings(user["id"])
    cargo_types = selected_cargo(settings)
    # Scan-time Fleet Size filter from the Dashboard scan form (empty =
    # no limit on that side); stored on the scan so a resume reuses it.
    min_pu = _parse_pu(request.form.get("min_pu") or body.get("min_pu"))
    max_pu = _parse_pu(request.form.get("max_pu") or body.get("max_pu"))
    scan_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    conn = store.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scans (user_id, scan_id, start_mc, end_mc, total,"
            " started_at, min_pu, max_pu) VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], scan_id, start_mc, end_mc, end_mc - start_mc + 1,
             now_str(), min_pu, max_pu))
        conn.commit()
        scan_db_id = cur.lastrowid
    finally:
        conn.close()
    stop = threading.Event()
    t = threading.Thread(target=run_scan,
                         args=(user["id"], scan_db_id, scan_id, start_mc,
                               end_mc, cargo_types, (min_pu, max_pu)),
                         daemon=True)
    RUNNING_SCANS[user["id"]] = {"stop": stop, "thread": t}
    t.start()
    return jsonify({"ok": True, "scan_id": scan_id})


@app.route("/api/stop-scan", methods=["POST"])
@login_required
def api_stop_scan():
    user = current_user()
    entry = RUNNING_SCANS.get(user["id"])
    if entry:
        entry["stop"].set()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "No scan is running."}), 400


@app.route("/api/resume-scan", methods=["POST"])
@login_required
def api_resume_scan():
    """Resume a Paused or Stopped scan from where it stopped — only when
    the user presses Resume. A scan never resumes on its own. A
    user-stopped scan keeps status 'Stopped' (it is not a stale
    leftover, which mark_stale_scans_paused marks 'Paused'), but both
    are resumable from current_mc."""
    user = current_user()
    if user["id"] in RUNNING_SCANS:
        return jsonify({"ok": False, "error": "A scan is already running."}), 400
    expired, _ = _demo_status(user)
    if expired:
        return jsonify({"ok": False,
                        "error": _expiry_message(user)}), 403
    conn = store.connect()
    try:
        scan = conn.execute(
            "SELECT * FROM scans WHERE user_id=? AND status IN ('Paused', 'Stopped')"
            " ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not scan:
            return jsonify({"ok": False,
                            "error": "No paused scan to resume."}), 400
        scan = dict(scan)
    finally:
        conn.close()
    if _demo_scan_too_big(user, scan["start_mc"], scan["end_mc"]):
        return jsonify({"ok": False,
                        "error": DEMO_SCAN_LIMIT_MESSAGE}), 403
    resume_from = (scan["current_mc"] or scan["start_mc"] - 1) + 1
    if resume_from > scan["end_mc"]:
        return jsonify({"ok": False,
                        "error": "That scan already finished."}), 400
    settings = get_settings(user["id"])
    cargo_types = selected_cargo(settings)
    # The paused scan keeps the Fleet Size range it started with.
    fleet = (scan.get("min_pu"), scan.get("max_pu"))
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE scans SET status='running', started_at=?,"
            " duration_seconds=0 WHERE id=?",
            (now_str(), scan["id"]))
        conn.commit()
    finally:
        conn.close()
    stop = threading.Event()
    t = threading.Thread(target=run_scan,
                         args=(user["id"], scan["id"], scan["scan_id"],
                               resume_from, scan["end_mc"], cargo_types,
                               fleet),
                         daemon=True)
    RUNNING_SCANS[user["id"]] = {"stop": stop, "thread": t}
    t.start()
    return jsonify({"ok": True, "scan_id": scan["scan_id"],
                    "resumed_from": resume_from})


@app.route("/api/scan-csv")
@login_required
def api_scan_csv():
    user = current_user()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM carriers WHERE user_id=? AND source='scan'"
            " ORDER BY mc_number", (user["id"],)).fetchall()
        return _carriers_csv([carrier_dict(r) for r in rows],
                             "qualified-carriers.csv")
    finally:
        conn.close()


def _full_address(c):
    """Full address for CSV export: street + city + state, skipping
    empty parts, joined with commas."""
    parts = [p.strip() for p in (c.get("address_line1") or "",
                                 c.get("city") or "",
                                 c.get("state") or "") if p.strip()]
    return ", ".join(parts)


def _carriers_csv(carriers, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["MC", "Legal Name", "Phone Number", "Address", "DBA"])
    for c in carriers:
        w.writerow(["MC-%s" % c["mc_number"], c.get("legal_name") or "",
                    c.get("phone") or "", _full_address(c),
                    c.get("dba_name") or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s" % filename})


# ---------------------------------------------------------------- carriers

def _parse_commodities(args):
    """Repeated ?commodity= params and/or comma-separated values ->
    list of known commodity names, de-duplicated, order preserved.
    Unknown names are dropped."""
    seen = set()
    out = []
    for v in args.getlist("commodity"):
        for x in (v or "").split(","):
            x = x.strip()
            if x and x not in seen and x in COMMODITIES:
                seen.add(x)
                out.append(x)
    return out


def _parse_pu(value):
    """Fleet-size bound: non-negative int, or None when empty/invalid
    (empty means no limit on that side). Accepts strings (form/query)
    and JSON numbers."""
    try:
        n = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _fleet_in_range(power_units, min_pu, max_pu):
    """Scan-time Fleet Size filter: True when the carrier's power units
    sit inside [min_pu, max_pu] (a None bound means no limit on that
    side). A carrier with unknown power units cannot be shown to be in
    range, so it does not qualify while any bound is set."""
    if min_pu is None and max_pu is None:
        return True
    pu = _parse_pu(power_units)
    if pu is None:
        return False
    if min_pu is not None and pu < min_pu:
        return False
    if max_pu is not None and pu > max_pu:
        return False
    return True


def _carrier_filters(args):
    return {
        "q": (args.get("q") or "").strip(),
        "state": (args.get("state") or "").strip().upper(),
        "cargo": (args.get("cargo") or "").strip().lower(),
        "commodities": _parse_commodities(args),
        "sort": (args.get("sort") or "mc").strip(),
        "starred": args.get("starred") == "1",
    }


def _export_args(args, f):
    """Query args for the Export CSV link, minus 'export' — keeping all
    repeated ?commodity= values (request.args.items() keeps only the first)."""
    out = {k: v for k, v in args.items() if k != "export"}
    if f["commodities"]:
        out["commodity"] = f["commodities"]
    return out


def _query_carriers(user_id, source, f):
    conn = store.connect()
    try:
        sql = "SELECT * FROM carriers WHERE user_id=? AND source=?"
        params = [user_id, source]
        if f["state"]:
            sql += " AND state=?"
            params.append(f["state"])
        if f["cargo"]:
            sql += " AND cargo_categories LIKE ?"
            params.append('%"' + f["cargo"] + '"%')
        if f["starred"]:
            sql += " AND starred=1"
        if f["q"]:
            sql += (" AND (legal_name LIKE ? OR mc_number LIKE ?"
                    " OR phone LIKE ? OR city LIKE ?)")
            like = "%" + f["q"] + "%"
            params += [like, like, like, like]
        order = {"mc": "CAST(mc_number AS INTEGER)",
                 "name": "legal_name", "state": "state, legal_name",
                 "pu": "CAST(power_units AS INTEGER) DESC"}.get(f["sort"], "CAST(mc_number AS INTEGER)")
        sql += " ORDER BY " + order
        rows = [carrier_dict(r) for r in conn.execute(sql, params).fetchall()]
        if f["commodities"]:
            rows = filter_carriers_by_commodity(rows, f["commodities"])
        states = [r["state"] for r in conn.execute(
            "SELECT DISTINCT state FROM carriers WHERE user_id=? AND source=?"
            " AND state != '' ORDER BY state", (user_id, source)).fetchall()]
        return rows, states
    finally:
        conn.close()


@app.route("/qualified")
@login_required
def qualified():
    user = current_user()
    f = _carrier_filters(request.args)
    rows, states = _query_carriers(user["id"], "scan", f)
    if request.args.get("export") == "csv":
        return _carriers_csv(rows, "qualified-carriers.csv")
    # L6: drop a caller-supplied "export" arg before re-passing the query
    # string to url_for (a duplicate keyword would 500).
    export_args = _export_args(request.args, f)
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           commodity_trucks={c: equipment_label(c)
                                             for c in commodity_names()},
                           title="Qualified Carriers", source="scan",
                           export_url=url_for("qualified", export="csv", **export_args))


@app.route("/api/carrier/<int:cid>/star", methods=["POST"])
@login_required
def api_star(cid):
    user = current_user()
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM carriers WHERE id=? AND user_id=?",
                           (cid, user["id"])).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        conn.execute("UPDATE carriers SET starred=? WHERE id=?",
                     (0 if row["starred"] else 1, cid))
        conn.commit()
        return jsonify({"ok": True, "starred": 0 if row["starred"] else 1})
    finally:
        conn.close()


@app.route("/api/carrier/<int:cid>/call-status", methods=["POST"])
@login_required
def api_call_status(cid):
    user = current_user()
    status = (request.json or {}).get("status", "none")
    if status not in ("call", "callback", "dnc", "none"):
        return jsonify({"ok": False}), 400
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM carriers WHERE id=? AND user_id=?",
                           (cid, user["id"])).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        calls = row["calls"] + (1 if status == "call" else 0)
        conn.execute("UPDATE carriers SET call_status=?, calls=? WHERE id=?",
                     (status, calls, cid))
        conn.commit()
        return jsonify({"ok": True, "calls": calls})
    finally:
        conn.close()


# ---------------------------------------------------------------- motus

@app.route("/motus-search", methods=["GET", "POST"])
@login_required
def motus_search():
    return _motus_page("motus", "Motus Search")


@app.route("/motus-register-search", methods=["GET", "POST"])
@login_required
def motus_register_search():
    return _motus_page("register", "Motus Register Search")


def _motus_page(kind, title):
    user = current_user()
    if request.method == "POST":
        expired, _ = _demo_status(user)
        if expired:
            flash(_expiry_message(user))
            return redirect(request.path)
        # H5: at most 10 Motus runs per user per hour.
        if _rate_limited(("motus", user["id"]), 10, 3600):
            flash("Too many Motus runs started — try again later.")
            return redirect(request.path)
        # L5: the one-run check lives in the database, not only in process
        # memory, so it holds across the 2 gunicorn workers.
        conn = store.connect()
        try:
            stale = conn.execute(
                "SELECT id FROM motus_runs WHERE user_id=? AND status='running'"
                " LIMIT 1", (user["id"],)).fetchone()
            if stale and user["id"] not in RUNNING_MOTUS:
                conn.execute("UPDATE motus_runs SET status='Stopped'"
                             " WHERE id=?", (stale["id"],))
                conn.commit()
                stale = None
        finally:
            conn.close()
        if stale or user["id"] in RUNNING_MOTUS:
            flash("A Motus run is already in progress.")
            return redirect(request.path)
        raw = request.form.get("usdots") or ""
        usdots = sorted({re.sub(r"\D", "", x) for x in re.split(r"[\s,;]+", raw) if x.strip()})
        usdots = [u for u in usdots if u]
        if not usdots:
            flash("Paste at least one USDOT number.")
            return redirect(request.path)
        if len(usdots) > 5000:
            flash("At most 5,000 USDOTs per run.")
            return redirect(request.path)
        settings = get_settings(user["id"])
        conn = store.connect()
        try:
            cur = conn.execute(
                "INSERT INTO motus_runs (user_id, kind, label, total, created_at)"
                " VALUES (?,?,?,?,?)",
                (user["id"], kind,
                 "%s USDOTs pasted" % len(usdots), len(usdots), now_str()))
            conn.commit()
            run_db_id = cur.lastrowid
        finally:
            conn.close()
        stop = threading.Event()
        t = threading.Thread(target=run_motus,
                             args=(user["id"], run_db_id, kind, usdots,
                                   selected_cargo(settings)), daemon=True)
        RUNNING_MOTUS[user["id"]] = {"stop": stop, "thread": t}
        t.start()
        return redirect(request.path)
    conn = store.connect()
    try:
        runs = conn.execute(
            "SELECT * FROM motus_runs WHERE user_id=? AND kind=? ORDER BY id DESC LIMIT 10",
            (user["id"], kind)).fetchall()
        log = conn.execute(
            "SELECT * FROM scan_log WHERE user_id=? AND scan_id LIKE 'motus-%'"
            " ORDER BY id DESC LIMIT 25", (user["id"],)).fetchall()
        return render_template("motus.html", user=user, title=title, kind=kind,
                               runs=[dict(r) for r in runs],
                               log=[dict(r) for r in log],
                               running=user["id"] in RUNNING_MOTUS)
    finally:
        conn.close()


@app.route("/api/motus-status")
@login_required
def api_motus_status():
    user = current_user()
    kind = request.args.get("kind", "motus")
    conn = store.connect()
    try:
        run = conn.execute(
            "SELECT * FROM motus_runs WHERE user_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (user["id"], kind)).fetchone()
        if not run:
            return jsonify({"running": False, "checked": 0, "total": 0,
                            "qualified": 0, "errors": 0})
        run = dict(run)
        log = conn.execute(
            "SELECT * FROM scan_log WHERE user_id=? AND scan_id=? "
            "ORDER BY id DESC LIMIT 25", (user["id"], "motus-%s" % run["id"])).fetchall()
        return jsonify({
            "running": user["id"] in RUNNING_MOTUS,
            "checked": run["checked"], "total": run["total"],
            "qualified": run["qualified"], "errors": run["errors"],
            "status": run["status"],
            "recent_log": [{
                "mc_number": "MC-%s" % r["mc_number"], "status": r["status"],
                "entity": r["entity"], "power_units": r["power_units"],
                "cargo": r["cargo"], "checked_at": r["checked_at"],
                "note": r["note"]} for r in log]})
    finally:
        conn.close()


@app.route("/api/motus-stop", methods=["POST"])
@login_required
def api_motus_stop():
    user = current_user()
    entry = RUNNING_MOTUS.get(user["id"])
    if entry:
        entry["stop"].set()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "No Motus run is running."}), 400


@app.route("/motus-qualified")
@login_required
def motus_qualified():
    user = current_user()
    f = _carrier_filters(request.args)
    rows, states = _query_carriers(user["id"], "motus", f)
    if request.args.get("export") == "csv":
        return _carriers_csv(rows, "motus-qualified.csv")
    export_args = _export_args(request.args, f)
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           commodity_trucks={c: equipment_label(c)
                                             for c in commodity_names()},
                           title="Motus Qualified", source="motus",
                           export_url=url_for("motus_qualified", export="csv", **export_args))


@app.route("/register-qualified")
@login_required
def register_qualified():
    user = current_user()
    f = _carrier_filters(request.args)
    rows, states = _query_carriers(user["id"], "register", f)
    if request.args.get("export") == "csv":
        return _carriers_csv(rows, "register-qualified.csv")
    export_args = _export_args(request.args, f)
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           commodity_trucks={c: equipment_label(c)
                                             for c in commodity_names()},
                           title="Register Qualified", source="register",
                           export_url=url_for("register_qualified", export="csv", **export_args))


# ---------------------------------------------------------------- restarted / saved / notes / pickup lines

@app.route("/restarted-search")
@login_required
def restarted():
    user = current_user()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT mc_number, authority_status, checked_at FROM check_history"
            " WHERE user_id=? ORDER BY mc_number, id", (user["id"],)).fetchall()
        by_mc = {}
        for r in rows:
            by_mc.setdefault(r["mc_number"], []).append(r)
        restarted = []
        for mc, hist in by_mc.items():
            auths = [h["authority_status"] or "" for h in hist]
            ever_not_auth = any(a and not a.upper().startswith("AUTHORIZED FOR")
                                for a in auths)
            last_auth = auths[-1] if auths else ""
            if ever_not_auth and last_auth.upper().startswith("AUTHORIZED FOR"):
                carrier = conn.execute(
                    "SELECT * FROM carriers WHERE user_id=? AND mc_number=?"
                    " ORDER BY id DESC LIMIT 1", (user["id"], mc)).fetchone()
                restarted.append({
                    "mc_number": mc,
                    "carrier": carrier_dict(carrier) if carrier else None,
                    "history": ["%s — %s" % (h["checked_at"],
                                              h["authority_status"] or "—")
                                for h in hist],
                })
        return render_template("restarted.html", user=user, rows=restarted)
    finally:
        conn.close()


@app.route("/saved")
@login_required
def saved():
    user = current_user()
    conn = store.connect()
    try:
        rows = [carrier_dict(r) for r in conn.execute(
            "SELECT * FROM carriers WHERE user_id=? AND starred=1"
            " ORDER BY CAST(mc_number AS INTEGER)", (user["id"],)).fetchall()]
        return render_template("saved.html", user=user, rows=rows,
                               commodities=commodity_names())
    finally:
        conn.close()


@app.route("/notes", methods=["GET", "POST"])
@login_required
def notes():
    user = current_user()
    if request.method == "POST":
        # M7: sane length caps on free-text fields.
        mc_number = (request.form.get("mc_number") or "").strip()[:20]
        title = (request.form.get("title") or "").strip()[:200]
        text = (request.form.get("text") or "").strip()
        tags = (request.form.get("tags") or "").strip()[:200]
        if len(text) > 5000:
            flash("Note text is too long — at most 5,000 characters.")
            return redirect(url_for("notes"))
        conn = store.connect()
        try:
            # Audit fix 10: whitelist the priority — anything else falls
            # back to Normal instead of being stored verbatim.
            priority = request.form.get("priority") or "Normal"
            if priority not in ("Low", "Normal", "High", "Urgent"):
                priority = "Normal"
            conn.execute(
                "INSERT INTO notes (user_id, mc_number, title, text, tags, priority,"
                " reminder_date, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user["id"], mc_number, title, text, tags,
                 priority,
                 request.form.get("reminder_date") or "", now_str()))
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for("notes"))
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM notes WHERE user_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        return render_template("notes.html", user=user, rows=[dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/notes/<int:nid>/delete", methods=["POST"])
@login_required
def delete_note(nid):
    user = current_user()
    conn = store.connect()
    try:
        conn.execute("DELETE FROM notes WHERE id=? AND user_id=?", (nid, user["id"]))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("notes"))


@app.route("/pickup-lines")
@login_required
def pickup_lines():
    user = current_user()
    category = (request.args.get("category") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    lines = lines_for(category if category in CATEGORIES else None)
    if q:
        lines = [(c, t) for c, t in lines if q in t.lower()]
    return render_template("pickup_lines.html", user=user, lines=lines,
                           categories=CATEGORIES, category=category, q=q)


@app.route("/cargo-guide")
@login_required
def cargo_guide():
    """Cargo Type Guide: pick a truck, see the commodities it hauls.

    Each commodity links to the Qualified page filtered by it, so the
    guide is actionable. Unknown ?equipment= values fall back to All.
    """
    user = current_user()
    equipment = (request.args.get("equipment") or "").strip()
    if equipment not in EQUIPMENT_COMMODITIES:
        equipment = ""
    guide = {eq: sorted(comms)
             for eq, comms in EQUIPMENT_COMMODITIES.items()}
    return render_template("cargo_guide.html", user=user,
                           equipment_list=sorted(guide.keys()),
                           guide=guide, equipment=equipment)


# ---------------------------------------------------------------- history / errors / reports / settings / about

@app.route("/history")
@login_required
def history():
    user = current_user()
    conn = store.connect()
    try:
        scans = conn.execute(
            "SELECT * FROM scans WHERE user_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        runs = conn.execute(
            "SELECT * FROM motus_runs WHERE user_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        return render_template("history.html", user=user,
                               scans=[dict(r) for r in scans],
                               runs=[dict(r) for r in runs])
    finally:
        conn.close()


@app.route("/errors")
@login_required
def errors_page():
    user = current_user()
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM error_mcs WHERE user_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        return render_template("errors.html", user=user, rows=[dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/errors/<int:eid>/retry", methods=["POST"])
@login_required
def retry_error(eid):
    user = current_user()
    settings = get_settings(user["id"])
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM error_mcs WHERE id=? AND user_id=?",
                           (eid, user["id"])).fetchone()
        if not row:
            abort(404)
        rec = check_mc(row["mc_number"], selected_cargo(settings),
                       direct_first=True)
        conn.execute("DELETE FROM error_mcs WHERE id=?", (eid,))
        if rec["qualified"]:
            conn.execute(
                "INSERT INTO carriers (user_id, scan_id, source, mc_number,"
                " usdot_number, legal_name, dba_name, entity_type, authority_status,"
                " cargo_carried, cargo_categories, address_line1, city, state, phone,"
                " power_units, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user["id"], row["scan_id"], row["source"], rec["mc_number"],
                 rec.get("usdot_number") or "", rec.get("legal_name") or "",
                 rec.get("dba_name") or "", rec.get("entity_type") or "",
                 rec.get("authority_status") or "",
                 json.dumps(rec.get("cargo_carried") or []),
                 json.dumps(rec.get("cargo_categories") or []),
                 rec.get("address_line1") or "", rec.get("city") or "",
                 rec.get("state") or "", rec.get("phone") or "",
                 rec.get("power_units") or "", now_str()))
        elif rec["status"] == "Error":
            conn.execute(
                "INSERT INTO error_mcs (user_id, scan_id, source, mc_number, error,"
                " checked_at) VALUES (?,?,?,?,?,?)",
                (user["id"], row["scan_id"], row["source"], row["mc_number"],
                 rec.get("note") or "Fetch error", now_str()))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("errors_page"))


@app.route("/reports")
@login_required
def reports():
    user = current_user()
    conn = store.connect()
    try:
        totals = conn.execute(
            "SELECT COUNT(*) scans, COALESCE(SUM(checked),0) checked,"
            " COALESCE(SUM(qualified),0) qualified, COALESCE(SUM(errors),0) errors"
            " FROM scans WHERE user_id=?", (user["id"],)).fetchone()
        carriers = conn.execute(
            "SELECT cargo_categories, state FROM carriers WHERE user_id=?",
            (user["id"],)).fetchall()
        cargo_tally = {"general": 0, "fresh": 0, "reefer": 0}
        state_tally = {}
        for c in carriers:
            for cat in json.loads(c["cargo_categories"] or "[]"):
                if cat in cargo_tally:
                    cargo_tally[cat] += 1
            if c["state"]:
                state_tally[c["state"]] = state_tally.get(c["state"], 0) + 1
        daily = conn.execute(
            "SELECT substr(started_at,1,10) d, SUM(checked) checked,"
            " SUM(qualified) qualified FROM scans WHERE user_id=?"
            " GROUP BY d ORDER BY d DESC LIMIT 14", (user["id"],)).fetchall()
        saved = conn.execute(
            "SELECT COUNT(*) c FROM carriers WHERE user_id=? AND starred=1",
            (user["id"],)).fetchone()["c"]
        checked = totals["checked"] or 0
        qualified = totals["qualified"] or 0
        rate = round(qualified / checked * 100, 1) if checked else 0
        return render_template(
            "reports.html", user=user, checked=checked, qualified=qualified,
            errors=totals["errors"] or 0, rate=rate, cargo_tally=cargo_tally,
            top_states=sorted(state_tally.items(), key=lambda kv: -kv[1])[:10],
            daily=[dict(r) for r in daily], saved=saved)
    finally:
        conn.close()


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    user = current_user()
    settings = get_settings(user["id"])
    message = ""
    password_changed = False
    if request.method == "POST":
        section = request.form.get("section", "appearance")
        conn = store.connect()
        try:
            if section == "appearance":
                # Audit fix 10: whitelist the allowed values — anything
                # else falls back to the default instead of being stored.
                theme = request.form.get("theme") or "Light"
                layout = request.form.get("layout") or "Comfortable"
                sidebar = request.form.get("sidebar") or "Expanded"
                if theme not in ("Light", "Dark", "System"):
                    theme = "Light"
                if layout not in ("Comfortable", "Compact"):
                    layout = "Comfortable"
                if sidebar not in ("Expanded", "Collapsed"):
                    sidebar = "Expanded"
                conn.execute(
                    "UPDATE settings SET theme=?, layout=?, sidebar=? WHERE user_id=?",
                    (theme, layout, sidebar, user["id"]))
                message = "Appearance saved."
            elif section == "notifications":
                conn.execute(
                    "UPDATE settings SET notify_email=?, notify_scan=?,"
                    " notify_qualified=?, notify_weekly=? WHERE user_id=?",
                    (1 if request.form.get("notify_email") else 0,
                     1 if request.form.get("notify_scan") else 0,
                     1 if request.form.get("notify_qualified") else 0,
                     1 if request.form.get("notify_weekly") else 0,
                     user["id"]))
                message = "Notification settings saved."
            elif section == "password":
                cur = request.form.get("current_password") or ""
                new = request.form.get("new_password") or ""
                if not check_password_hash(user["password_hash"], cur):
                    message = "Current password is incorrect."
                elif len(new) < MIN_PASSWORD_LEN or len(new) > 128:
                    message = "New password must be %d–128 characters." % MIN_PASSWORD_LEN
                else:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (generate_password_hash(new), user["id"]))
                    message = "Password changed."
                    password_changed = True
            conn.commit()
        finally:
            conn.close()
        if password_changed:
            # M2: changing the password kills all other sessions. (Done
            # after commit, on a fresh connection — the settings
            # connection above still holds a write lock here.)
            _bump_session_version(user["id"])
            session["sv"] = user["session_version"] + 1
        settings = get_settings(user["id"])
    demo_expired, demo_days_left = _demo_status(dict(user))
    return render_template("settings.html", user=user, settings=settings,
                           message=message, demo_expired=demo_expired,
                           demo_days_left=demo_days_left,
                           access_end_date=_format_access_end(
                               _access_expiry(dict(user))))


@app.route("/settings/export")
@login_required
def export_data():
    user = current_user()
    conn = store.connect()
    try:
        data = {}
        for table in ("scans", "carriers", "notes", "error_mcs", "motus_runs",
                      "check_history"):
            data[table] = [dict(r) for r in conn.execute(
                "SELECT * FROM %s WHERE user_id=?" % table, (user["id"],)).fetchall()]
        return Response(json.dumps(data, indent=2), mimetype="application/json",
                        headers={"Content-Disposition":
                                 "attachment; filename=roadx-data-export.json"})
    finally:
        conn.close()


@app.route("/settings/delete-account", methods=["POST"])
@login_required
def delete_account():
    user = current_user()
    conn = store.connect()
    try:
        _delete_user_data(conn, user["id"])
        conn.commit()
    finally:
        conn.close()
    session.clear()
    # Mark the fresh session: a retry of the same delete form (its CSRF
    # token died with the old session) is recognised in the 403 handler
    # as "already deleted" instead of a scary session-expired error.
    # _login_user clears the session on the next login, so this flag
    # never survives into a new account session.
    session["account_deleted"] = True
    flash("Your account has been deleted.")
    return redirect(url_for("login"))


@app.route("/about")
@login_required
def about():
    return render_template("about.html", user=current_user())


@app.route("/api/reference")
@login_required
def api_reference():
    return jsonify({
        "commodities": COMMODITIES,
        "commodity_names": commodity_names(),
        "pickup_lines": [{"category": c, "line": t} for c, t in PICKUP_LINES],
    })


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin_page():
    user = current_user()
    conn = store.connect()
    try:
        users = conn.execute(
            "SELECT id, email, username, is_admin, plan, blocked, created_at,"
            " access_expires_at, last_login, login_count FROM users"
            " ORDER BY id").fetchall()
        events = conn.execute(
            "SELECT * FROM login_events ORDER BY id DESC LIMIT 50").fetchall()
        resets = conn.execute(
            "SELECT pr.*, u.email, u.username FROM password_resets pr"
            " JOIN users u ON u.id=pr.user_id"
            " ORDER BY pr.id DESC LIMIT 20").fetchall()
        audit = conn.execute(
            "SELECT a.*, u.username AS admin_name, t.username AS target_name"
            " FROM admin_audit a"
            " LEFT JOIN users u ON u.id=a.admin_id"
            " LEFT JOIN users t ON t.id=a.target_user_id"
            " ORDER BY a.id DESC LIMIT 20").fetchall()
        # Opt-in TOTP 2FA (admin only, off by default). A pending setup
        # secret lives in the session until the admin confirms it with
        # a code — it is never written to the database unconfirmed.
        totp_enabled = _totp_enabled(user)
        totp_pending = session.get("totp_pending")
        totp_uri = None
        if totp_pending and pyotp:
            totp_uri = pyotp.TOTP(totp_pending).provisioning_uri(
                name=user["email"], issuer_name="RoadxSolution")
        return render_template("admin.html", user=user,
                               users=[dict(r, access_ends=(
                                   "Never" if r["is_admin"]
                                   or r["plan"] == "Lifetime"
                                   else (_format_access_end(
                                       _access_expiry(dict(r)))
                                       or "—")))
                                   for r in users],
                               events=[dict(r) for r in events],
                               resets=[dict(r) for r in resets],
                               audit=[dict(r) for r in audit],
                               totp_enabled=totp_enabled,
                               totp_pending=totp_pending,
                               totp_uri=totp_uri)
    finally:
        conn.close()


@app.route("/admin/set-password", methods=["POST"])
@admin_required
def admin_set_password():
    admin = current_user()
    uid = request.form.get("user_id")
    new = request.form.get("new_password") or ""
    # L6: a non-integer user id used to raise a 500.
    try:
        uid_int = int(uid) if uid else None
    except (TypeError, ValueError):
        uid_int = None
    if not uid_int or len(new) < MIN_PASSWORD_LEN or len(new) > 128:
        flash("Select a user and a password of %d–128 characters." % MIN_PASSWORD_LEN)
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        target = conn.execute("SELECT id FROM users WHERE id=?",
                              (uid_int,)).fetchone()
        if not target:
            flash("No such user.")
            return redirect(url_for("admin_page"))
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (generate_password_hash(new), uid_int))
        conn.commit()
    finally:
        conn.close()
    # M2 + L7: the target's other sessions die, and the change is logged.
    _bump_session_version(uid_int)
    _log_admin_action(admin["id"], uid_int, "admin_set_password")
    flash("Password updated.")
    return redirect(url_for("admin_page"))


@app.route("/admin/set-plan", methods=["POST"])
@admin_required
def admin_set_plan():
    """Give a user Lifetime access or put them back on a Demo trial.

    The admin account itself always stays Lifetime. A plan change takes
    effect on the user's next request — plans are read fresh from the
    database on every request, so no session invalidation is needed.
    The access clock moves with the plan: Demo restarts a fresh 7-day
    trial from now, Lifetime clears the expiry. (The Admin page popup
    /admin/set-access is the full duration picker; this route stays for
    the plain Demo/Lifetime switch.)"""
    admin = current_user()
    uid = request.form.get("user_id")
    plan = (request.form.get("plan") or "").strip()
    try:
        uid_int = int(uid) if uid else None
    except (TypeError, ValueError):
        uid_int = None
    if not uid_int or plan not in ("Demo", "Lifetime"):
        flash("Select a user and a plan (Demo or Lifetime).")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        target = conn.execute("SELECT id, is_admin FROM users WHERE id=?",
                              (uid_int,)).fetchone()
        if not target:
            flash("No such user.")
            return redirect(url_for("admin_page"))
        if target["is_admin"]:
            flash("The admin account stays Lifetime.")
            return redirect(url_for("admin_page"))
        expires = (_future_str(DEMO_TRIAL_DAYS) if plan == "Demo"
                   else None)
        conn.execute("UPDATE users SET plan=?, access_expires_at=? "
                     "WHERE id=?", (plan, expires, uid_int))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(admin["id"], uid_int,
                      "admin_set_plan_%s" % plan.lower())
    flash("Plan updated to %s." % plan)
    return redirect(url_for("admin_page"))


@app.route("/admin/set-access", methods=["POST"])
@admin_required
def admin_set_access():
    """Owner-only access-duration picker (Admin page popup).

    Choices: Demo (7-day trial), 1 month (30 days), 6 months
    (180 days), 1 year (365 days), Lifetime (never expires), or a
    custom number of months (decimals allowed — 1.5 months is 45
    days; a month is 30 days). The timer starts the moment the owner
    picks: access_expires_at is set to now + the chosen length, and
    the plan label moves with it. The admin account itself can never
    be changed — it stays Lifetime. Users never see this control:
    their Dashboard/Settings only show the read-only end date."""
    admin = current_user()
    uid = request.form.get("user_id")
    duration = (request.form.get("duration") or "").strip()
    try:
        uid_int = int(uid) if uid else None
    except (TypeError, ValueError):
        uid_int = None
    if not uid_int or duration not in (
            "demo", "1month", "6months", "1year", "lifetime", "custom"):
        flash("Select a user and an access duration.")
        return redirect(url_for("admin_page"))
    days = None
    if duration == "custom":
        try:
            months = float((request.form.get("custom_months") or "")
                           .strip())
        except (TypeError, ValueError):
            months = None
        if months is None or months <= 0 or months > 120:
            flash("Custom access must be more than 0 and at most "
                  "120 months.")
            return redirect(url_for("admin_page"))
        days = months * 30
    elif duration in ACCESS_DURATIONS:
        days = ACCESS_DURATIONS[duration]
    conn = store.connect()
    try:
        target = conn.execute("SELECT id, username, is_admin FROM users"
                              " WHERE id=?", (uid_int,)).fetchone()
        if not target:
            flash("No such user.")
            return redirect(url_for("admin_page"))
        if target["is_admin"]:
            flash("The admin account stays Lifetime.")
            return redirect(url_for("admin_page"))
        name = target["username"]
        label = ACCESS_PLAN_LABELS[duration]
        if duration == "lifetime":
            expires = None
            ends_text = "never expires"
        else:
            expires = (datetime.now() + timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S")
            ends_text = "ends %s" % _format_access_end(
                datetime.strptime(expires, "%Y-%m-%d %H:%M:%S"))
        conn.execute("UPDATE users SET plan=?, access_expires_at=? "
                     "WHERE id=?", (label, expires, uid_int))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(admin["id"], uid_int,
                      "admin_set_access_%s" % duration)
    flash("Access set to %s for %s — %s." % (label, name, ends_text))
    return redirect(url_for("admin_page"))


@app.route("/admin/block-user", methods=["POST"])
@admin_required
def admin_block_user():
    """Owner block: end a user's access at once. Their data and plan
    stay untouched — only access is disabled — so unblock restores
    exactly what they had. All of their sessions die immediately
    (session_version bump, same mechanism as a password change)."""
    admin = current_user()
    uid = request.form.get("user_id")
    try:
        uid_int = int(uid) if uid else None
    except (TypeError, ValueError):
        uid_int = None
    if not uid_int:
        flash("Select a user to block.")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        target = conn.execute(
            "SELECT id, username, is_admin, blocked FROM users WHERE id=?",
            (uid_int,)).fetchone()
        if not target:
            flash("No such user.")
            return redirect(url_for("admin_page"))
        if target["is_admin"]:
            flash("Admin accounts cannot be blocked.")
            return redirect(url_for("admin_page"))
        if target["blocked"]:
            flash("That user is already blocked.")
            return redirect(url_for("admin_page"))
        name = target["username"]
        conn.execute("UPDATE users SET blocked=1 WHERE id=?", (uid_int,))
        conn.commit()
    finally:
        conn.close()
    _bump_session_version(uid_int)
    _log_admin_action(admin["id"], uid_int, "admin_block_user")
    flash("Access blocked for %s." % name)
    return redirect(url_for("admin_page"))


@app.route("/admin/unblock-user", methods=["POST"])
@admin_required
def admin_unblock_user():
    """Restore a blocked user's access fully — their plan and data
    were never touched, so they come back exactly as they were. Their
    old sessions stay dead (the block's version bump is not undone),
    so they sign in fresh."""
    admin = current_user()
    uid = request.form.get("user_id")
    try:
        uid_int = int(uid) if uid else None
    except (TypeError, ValueError):
        uid_int = None
    if not uid_int:
        flash("Select a user to unblock.")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        target = conn.execute(
            "SELECT id, username, is_admin, blocked FROM users WHERE id=?",
            (uid_int,)).fetchone()
        if not target:
            flash("No such user.")
            return redirect(url_for("admin_page"))
        if target["is_admin"]:
            flash("Admin accounts cannot be blocked.")
            return redirect(url_for("admin_page"))
        if not target["blocked"]:
            flash("That user is not blocked.")
            return redirect(url_for("admin_page"))
        name = target["username"]
        conn.execute("UPDATE users SET blocked=0 WHERE id=?", (uid_int,))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(admin["id"], uid_int, "admin_unblock_user")
    flash("Access restored for %s." % name)
    return redirect(url_for("admin_page"))


@app.route("/admin/create-user", methods=["POST"])
@admin_required
def admin_create_user():
    """Create a demo account for one person — same rules as signup.

    The account is always plan 'Demo' (a 7-day full trial) and never an
    admin. If the admin leaves the password blank, a random 16-character
    password is generated so demo accounts never share one known
    password — it is shown to the admin ONCE in the flash message below
    (only its hash is stored), so the admin can share it with the
    person right away. A typed password still works, with the same
    8–128 character rule as everywhere else."""
    admin = current_user()
    email = (request.form.get("email") or "").strip().lower()[:254]
    username = (request.form.get("username") or "").strip()[:50]
    password = request.form.get("password") or ""
    generated = False
    if not password:
        password = _random_demo_password()
        generated = True
    if not email or not username or \
            len(password) < MIN_PASSWORD_LEN or len(password) > 128:
        flash("Email, username and a password of %d–128 characters are"
              " required." % MIN_PASSWORD_LEN)
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        if conn.execute("SELECT id FROM users WHERE email=? OR username=?",
                        (email, username)).fetchone():
            flash("An account with this email or username already exists.")
            return redirect(url_for("admin_page"))
        # Audit fix 4: same double-submit race as signup — catch the
        # UNIQUE violation instead of 500'ing.
        try:
            cur = conn.execute(
                "INSERT INTO users (email, username, password_hash, is_admin,"
                " plan, created_at, access_expires_at) VALUES (?,?,?,?,?,?,?)",
                (email, username, generate_password_hash(password), 0, "Demo",
                 now_str(), _future_str(DEMO_TRIAL_DAYS)))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("An account with this email or username already exists.")
            return redirect(url_for("admin_page"))
        new_id = cur.lastrowid
    finally:
        conn.close()
    _log_admin_action(admin["id"], new_id, "admin_create_demo_user")
    if generated:
        # Shown once, on this redirect only — the password is never
        # stored or displayed again. The admin shares it with the
        # person now; afterwards only a reset can give them access.
        flash("Demo account created for %s — password: %s (shown once —"
              " share it with them now)." % (username, password))
    else:
        flash("Demo account created for %s — share the login with them."
              % username)
    return redirect(url_for("admin_page"))


# ---------------------------------------------------------------- 2FA (TOTP)

@app.route("/admin/2fa/setup", methods=["POST"])
@admin_required
def admin_2fa_setup():
    """Start 2FA setup: generate a TOTP secret and hold it in the
    session (NOT in the database) until the admin confirms it with a
    code from their authenticator app. The secret + otpauth:// URL are
    shown on the Admin page — the admin types the secret into their
    authenticator app by hand (no QR image is generated)."""
    admin = current_user()
    if not admin["is_admin"]:
        flash("Only the admin can use two-factor authentication.")
        return redirect(url_for("admin_page"))
    if _totp_enabled(admin):
        flash("Two-factor authentication is already enabled.")
        return redirect(url_for("admin_page"))
    if pyotp is None:
        flash("Two-factor authentication is not available right now.")
        return redirect(url_for("admin_page"))
    session["totp_pending"] = pyotp.random_base32()
    return redirect(url_for("admin_page"))


@app.route("/admin/2fa/confirm", methods=["POST"])
@admin_required
def admin_2fa_confirm():
    """Confirm 2FA setup with a current 6-digit code. Only then is the
    secret stored on the admin's account — an unconfirmed secret never
    reaches the database, so a half-finished setup cannot lock anyone
    out."""
    admin = current_user()
    if not admin["is_admin"]:
        flash("Only the admin can use two-factor authentication.")
        return redirect(url_for("admin_page"))
    pending = session.get("totp_pending")
    if not pending or pyotp is None:
        flash("Start 2FA setup first.")
        return redirect(url_for("admin_page"))
    code = request.form.get("totp_code") or ""
    if not _totp_verify(pending, code):
        flash("That code did not match — check your authenticator app"
              " and try again.")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        conn.execute("UPDATE users SET totp_secret=? WHERE id=?",
                     (pending, admin["id"]))
        conn.commit()
    finally:
        conn.close()
    session.pop("totp_pending", None)
    _log_admin_action(admin["id"], admin["id"], "admin_2fa_enabled")
    flash("Two-factor authentication enabled — your next sign-in will"
          " ask for a code from your authenticator app.")
    return redirect(url_for("admin_page"))


@app.route("/admin/2fa/cancel", methods=["POST"])
@admin_required
def admin_2fa_cancel():
    """Drop an unconfirmed 2FA setup — nothing was stored, so there is
    nothing to undo."""
    session.pop("totp_pending", None)
    return redirect(url_for("admin_page"))


@app.route("/admin/2fa/disable", methods=["POST"])
@admin_required
def admin_2fa_disable():
    """Turn 2FA off. Requires the admin's own password as confirmation
    — a stolen session alone must not be able to strip the second
    factor. Existing sessions keep working (2FA gates sign-in, not
    active sessions), so no session bump is needed here."""
    admin = current_user()
    if not admin["is_admin"]:
        flash("Only the admin can use two-factor authentication.")
        return redirect(url_for("admin_page"))
    password = request.form.get("password") or ""
    if not check_password_hash(admin["password_hash"], password):
        flash("Password is incorrect — 2FA was not disabled.")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        conn.execute("UPDATE users SET totp_secret=NULL WHERE id=?",
                     (admin["id"],))
        conn.commit()
    finally:
        conn.close()
    _log_admin_action(admin["id"], admin["id"], "admin_2fa_disabled")
    flash("Two-factor authentication disabled.")
    return redirect(url_for("admin_page"))


# ---------------------------------------------------------------- main

@app.route("/health")
def health():
    return jsonify({"ok": True, "app": "roadxsolution-mcsearch"})


if __name__ == "__main__":
    # L6: never start with the debugger on — it is remote code execution.
    if os.environ.get("FLASK_DEBUG") or \
            os.environ.get("FLASK_ENV") == "development":
        raise SystemExit(
            "Refusing to start: FLASK_DEBUG / FLASK_ENV=development is set.")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
