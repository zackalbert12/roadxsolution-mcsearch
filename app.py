"""RoadxSolution-mcsearch — standalone Flask app.

A dispatcher MC-verification workspace: scan FMCSA MC ranges live against
SAFER, keep qualified carriers, Motus USDOT checks, notes, pickup lines,
reports and admin user management. Deployable on Render's free tier.
"""
import concurrent.futures
import csv
import io
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import (
    Flask, request, session, redirect, url_for, render_template,
    jsonify, Response, abort, flash,
)
from werkzeug.security import generate_password_hash, check_password_hash

import db as store
from safer import check_mc, check_usdot, search_by_name
from commodity import COMMODITIES, commodity_names, filter_carriers_by_commodity
from pickup_lines import PICKUP_LINES, CATEGORIES, lines_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-secret-change-me"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

store.init_db()

# ---------------------------------------------------------------- helpers

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_time():
    return datetime.now().strftime("%H:%M:%S")


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = store.connect()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()


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
            conn.execute("INSERT INTO settings (user_id) VALUES (?)", (user_id,))
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
    d["cargo_carried"] = json.loads(d["cargo_carried"] or "[]")
    d["cargo_categories"] = json.loads(d["cargo_categories"] or "[]")
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


def run_scan(user_id, scan_db_id, scan_id, start_mc, end_mc, cargo_types):
    """Background thread: walk an MC range, checking each against SAFER.

    Checks run in parallel (ROAD_SCAN_WORKERS threads, default 5) with
    rotating proxies — about 5-7 checks/sec instead of ~1/sec.
    Submitted in small chunks so Stop stays responsive.
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
                chunk = mcs[chunk_start:chunk_start + 10]
                futs = {ex.submit(_safe_check_mc, mc, cargo_types): mc
                        for mc in chunk}
                for fut in concurrent.futures.as_completed(futs):
                    if stop.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    _write_scan_result(user_id, scan_db_id, scan_id,
                                       fut.result())
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
    matches, err = search_by_name(q)
    if err:
        rows = _local_carriers_by_name(q)
        if rows:
            return {"kind": "local_matches", "rows": rows,
                    "heading": "Saved carriers matching this name",
                    "note": "Live SAFER name search is unavailable right now (%s) — showing carriers we already checked." % err}
        return {"kind": "message",
                "text": "SAFER name search is unavailable right now (%s). Try an MC or USDOT number instead." % err}
    if not matches:
        rows = _local_carriers_by_name(q)
        if rows:
            return {"kind": "local_matches", "rows": rows,
                    "heading": "No live SAFER match — saved carriers matching this name"}
        return {"kind": "message",
                "text": "No carrier named \u201c%s\u201d found on SAFER, and none in our checked carriers either. Check the spelling or search by MC / USDOT." % q}
    if len(matches) == 1:
        rec = check_usdot(matches[0]["usdot"], ("general", "fresh", "reefer"))
        res = _detail_result(rec, "USDOT")
        res["matched"] = matches[0]
        return res
    return {"kind": "name_matches", "matches": matches,
            "total": len(matches)}


def _run_carrier_search(q, stype):
    """Run a public carrier search. Result kinds: detail | ambiguous |
    name_matches | local_matches | message."""
    if stype == "mc":
        return _detail_result(check_mc(q, ("general", "fresh", "reefer")), "MC_MX")
    if stype == "usdot":
        return _detail_result(check_usdot(q, ("general", "fresh", "reefer")), "USDOT")
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
        dot = check_usdot(stripped, ("general", "fresh", "reefer"))
        mc = check_mc(stripped, ("general", "fresh", "reefer"))

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
        # (e.g. an inactive one), DOT first, else the MC Not Found state.
        if dot.get("found") and not dot.get("fetch_error"):
            return _detail_result(dot, "USDOT")
        return _detail_result(mc, "MC_MX")
    if digits and (len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))):
        return _run_carrier_search(q, "phone")
    if stripped.isdigit():
        return _run_carrier_search(stripped, "mc")
    return _name_search(q)


@app.route("/search", methods=["GET", "POST"])
def carrier_search():
    """Public carrier search — no login needed. MC, USDOT, phone, email
    or company name; live SAFER data for numbers and names, saved
    carriers for phone/email reverse lookup."""
    user = current_user()
    q = (request.values.get("q") or "").strip()
    stype = (request.values.get("type") or "auto").strip().lower()
    if stype not in ("auto", "mc", "usdot", "phone", "email", "name"):
        stype = "auto"
    result = None
    if q:
        try:
            result = _run_carrier_search(q, stype)
        except Exception as exc:
            result = {"kind": "message",
                      "text": "Search failed: %s. Please try again." % exc}
    return render_template("search.html", user=user, q=q, stype=stype,
                           result=result)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        form = request.form.get("form", "login")
        conn = store.connect()
        try:
            if form == "signup":
                email = (request.form.get("email") or "").strip().lower()
                username = (request.form.get("username") or "").strip()
                password = request.form.get("password") or ""
                if not email or not username or len(password) < 6:
                    error = "Email, username and a password of 6+ characters are required."
                elif conn.execute("SELECT id FROM users WHERE email=? OR username=?",
                                  (email, username)).fetchone():
                    error = "An account with this email or username already exists. Try logging in instead."
                else:
                    is_admin = 1 if conn.execute(
                        "SELECT COUNT(*) c FROM users").fetchone()["c"] == 0 else 0
                    conn.execute(
                        "INSERT INTO users (email, username, password_hash, is_admin,"
                        " created_at) VALUES (?,?,?,?,?)",
                        (email, username, generate_password_hash(password),
                         is_admin, now_str()))
                    conn.commit()
                    user = conn.execute(
                        "SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    session["user_id"] = user["id"]
                    session.permanent = True
                    return redirect(url_for("dashboard"))
            else:
                ident = (request.form.get("identifier") or "").strip()
                password = request.form.get("password") or ""
                user = conn.execute(
                    "SELECT * FROM users WHERE email=? OR username=?",
                    (ident.lower(), ident)).fetchone()
                ok = bool(user and check_password_hash(user["password_hash"], password))
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
                    session["user_id"] = user["id"]
                    session.permanent = bool(request.form.get("remember"))
                    return redirect(url_for("dashboard"))
                conn.commit()
                error = "Invalid username/email or password."
        finally:
            conn.close()
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Request a password reset. The admin sees the one-time code in the
    Admin page and relays it to the user."""
    message = ""
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        conn = store.connect()
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user:
                error = "No account with that email."
            else:
                code = "%06d" % secrets.randbelow(1000000)
                expires = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO password_resets (user_id, code, created_at, expires_at)"
                    " VALUES (?,?,?,?)", (user["id"], code, now_str(), expires))
                conn.commit()
                message = ("Reset requested. Ask the site admin for your one-time code "
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
        email = (request.form.get("email") or "").strip().lower()
        code = (request.form.get("code") or "").strip()
        password = request.form.get("password") or ""
        conn = store.connect()
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            row = None
            if user:
                row = conn.execute(
                    "SELECT * FROM password_resets WHERE user_id=? AND code=? AND used=0"
                    " ORDER BY id DESC LIMIT 1", (user["id"], code)).fetchone()
            if not user or not row:
                error = "Invalid email or code."
            elif row["expires_at"] < now_str():
                error = "That code has expired. Request a new one."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            else:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                             (generate_password_hash(password), user["id"]))
                conn.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
                conn.commit()
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
        return render_template(
            "dashboard.html", user=user, settings=settings,
            scan=dict(scan) if scan else None,
            log=[dict(r) for r in log],
            total_scanned=totals["c"] or 0,
            commodities=commodity_names(),
            running=user["id"] in RUNNING_SCANS)
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
            elapsed = int((datetime.now() - datetime.strptime(
                scan["started_at"], "%Y-%m-%d %H:%M:%S")).total_seconds())
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
            "can_resume": scan["status"] == "Paused",
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
    if user["id"] in RUNNING_SCANS:
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
    settings = get_settings(user["id"])
    cargo_types = selected_cargo(settings)
    scan_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    conn = store.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scans (user_id, scan_id, start_mc, end_mc, total,"
            " started_at) VALUES (?,?,?,?,?,?)",
            (user["id"], scan_id, start_mc, end_mc, end_mc - start_mc + 1,
             now_str()))
        conn.commit()
        scan_db_id = cur.lastrowid
    finally:
        conn.close()
    stop = threading.Event()
    t = threading.Thread(target=run_scan,
                         args=(user["id"], scan_db_id, scan_id, start_mc,
                               end_mc, cargo_types), daemon=True)
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
    """Resume a Paused scan from where it stopped — only when the user
    presses Resume. A scan never resumes on its own."""
    user = current_user()
    if user["id"] in RUNNING_SCANS:
        return jsonify({"ok": False, "error": "A scan is already running."}), 400
    conn = store.connect()
    try:
        scan = conn.execute(
            "SELECT * FROM scans WHERE user_id=? AND status='Paused'"
            " ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        if not scan:
            return jsonify({"ok": False,
                            "error": "No paused scan to resume."}), 400
        scan = dict(scan)
    finally:
        conn.close()
    resume_from = (scan["current_mc"] or scan["start_mc"] - 1) + 1
    if resume_from > scan["end_mc"]:
        return jsonify({"ok": False,
                        "error": "That scan already finished."}), 400
    settings = get_settings(user["id"])
    cargo_types = selected_cargo(settings)
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
                               resume_from, scan["end_mc"], cargo_types),
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


def _carriers_csv(carriers, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["MC", "Legal Name", "DBA", "State", "Phone", "Power Units",
                "Cargo", "City", "Address", "Authority", "USDOT"])
    for c in carriers:
        w.writerow(["MC-%s" % c["mc_number"], c.get("legal_name") or "",
                    c.get("dba_name") or "", c.get("state") or "",
                    c.get("phone") or "", c.get("power_units") or "",
                    ", ".join(c.get("cargo_carried") or []),
                    c.get("city") or "", c.get("address_line1") or "",
                    c.get("authority_status") or "",
                    c.get("usdot_number") or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s" % filename})


# ---------------------------------------------------------------- carriers

def _carrier_filters(args):
    return {
        "q": (args.get("q") or "").strip(),
        "state": (args.get("state") or "").strip().upper(),
        "cargo": (args.get("cargo") or "").strip().lower(),
        "commodity": (args.get("commodity") or "").strip(),
        "sort": (args.get("sort") or "mc").strip(),
        "starred": args.get("starred") == "1",
    }


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
        if f["commodity"]:
            rows = filter_carriers_by_commodity(rows, f["commodity"])
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
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           title="Qualified Carriers", source="scan",
                           export_url=url_for("qualified", export="csv", **request.args))


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
        if user["id"] in RUNNING_MOTUS:
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
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           title="Motus Qualified", source="motus",
                           export_url=url_for("motus_qualified", export="csv", **request.args))


@app.route("/register-qualified")
@login_required
def register_qualified():
    user = current_user()
    f = _carrier_filters(request.args)
    rows, states = _query_carriers(user["id"], "register", f)
    if request.args.get("export") == "csv":
        return _carriers_csv(rows, "register-qualified.csv")
    return render_template("qualified.html", user=user, rows=rows, states=states,
                           f=f, commodities=commodity_names(),
                           title="Register Qualified", source="register",
                           export_url=url_for("register_qualified", export="csv", **request.args))


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
        conn = store.connect()
        try:
            conn.execute(
                "INSERT INTO notes (user_id, mc_number, title, text, tags, priority,"
                " reminder_date, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user["id"], (request.form.get("mc_number") or "").strip(),
                 (request.form.get("title") or "").strip(),
                 (request.form.get("text") or "").strip(),
                 (request.form.get("tags") or "").strip(),
                 request.form.get("priority") or "Normal",
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
        rec = check_mc(row["mc_number"], selected_cargo(settings))
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
    if request.method == "POST":
        section = request.form.get("section", "scanner")
        conn = store.connect()
        try:
            if section == "scanner":
                conn.execute(
                    "UPDATE settings SET"
                    " cargo_general=?, cargo_fresh=?, cargo_reefer=? WHERE user_id=?",
                    (1 if request.form.get("cargo_general") else 0,
                     1 if request.form.get("cargo_fresh") else 0,
                     1 if request.form.get("cargo_reefer") else 0,
                     user["id"]))
                message = "Scanner settings saved."
            elif section == "appearance":
                conn.execute(
                    "UPDATE settings SET theme=?, layout=?, sidebar=? WHERE user_id=?",
                    (request.form.get("theme") or "Light",
                     request.form.get("layout") or "Comfortable",
                     request.form.get("sidebar") or "Expanded", user["id"]))
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
                elif len(new) < 6:
                    message = "New password must be at least 6 characters."
                else:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (generate_password_hash(new), user["id"]))
                    message = "Password changed."
            conn.commit()
        finally:
            conn.close()
        settings = get_settings(user["id"])
    return render_template("settings.html", user=user, settings=settings,
                           message=message)


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
        uid = user["id"]
        for table in ("carriers", "scans", "scan_log", "check_history",
                      "error_mcs", "notes", "motus_runs", "login_events",
                      "password_resets", "settings"):
            conn.execute("DELETE FROM %s WHERE user_id=?" % table, (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    session.pop("user_id", None)
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
            "SELECT id, email, username, is_admin, plan, created_at, last_login,"
            " login_count FROM users ORDER BY id").fetchall()
        events = conn.execute(
            "SELECT * FROM login_events ORDER BY id DESC LIMIT 50").fetchall()
        resets = conn.execute(
            "SELECT pr.*, u.email, u.username FROM password_resets pr"
            " JOIN users u ON u.id=pr.user_id"
            " ORDER BY pr.id DESC LIMIT 20").fetchall()
        return render_template("admin.html", user=user,
                               users=[dict(r) for r in users],
                               events=[dict(r) for r in events],
                               resets=[dict(r) for r in resets])
    finally:
        conn.close()


@app.route("/admin/set-password", methods=["POST"])
@admin_required
def admin_set_password():
    uid = request.form.get("user_id")
    new = request.form.get("new_password") or ""
    if not uid or len(new) < 6:
        flash("Select a user and a password of 6+ characters.")
        return redirect(url_for("admin_page"))
    conn = store.connect()
    try:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (generate_password_hash(new), int(uid)))
        conn.commit()
    finally:
        conn.close()
    flash("Password updated.")
    return redirect(url_for("admin_page"))


# ---------------------------------------------------------------- main

@app.route("/health")
def health():
    return jsonify({"ok": True, "app": "roadxsolution-mcsearch"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
