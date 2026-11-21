"""SQLite storage for RoadxSolution-mcsearch.

One file (data/roadx.db). Created on first run; schema is idempotent.
Note: on Render's free tier the filesystem is ephemeral, so data resets
when the service restarts or redeploys. That is a free-tier limit, not a
bug — attach a persistent disk or a hosted database for durable storage.
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("ROAD_DB_PATH") or os.path.join(DATA_DIR, "roadx.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    plan TEXT NOT NULL DEFAULT 'Lifetime',
    created_at TEXT NOT NULL,
    last_login TEXT,
    login_count INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    target_user_id INTEGER,
    action TEXT NOT NULL,
    ip TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    identifier TEXT,
    success INTEGER NOT NULL,
    ip TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scan_id TEXT NOT NULL,
    start_mc INTEGER NOT NULL,
    end_mc INTEGER NOT NULL,
    current_mc INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    not_found INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS carriers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scan_id TEXT,
    source TEXT NOT NULL DEFAULT 'scan',
    mc_number TEXT NOT NULL,
    usdot_number TEXT,
    legal_name TEXT,
    dba_name TEXT,
    entity_type TEXT,
    authority_status TEXT,
    cargo_carried TEXT,
    cargo_categories TEXT,
    address_line1 TEXT,
    city TEXT,
    state TEXT,
    phone TEXT,
    power_units TEXT,
    qualified INTEGER NOT NULL DEFAULT 1,
    starred INTEGER NOT NULL DEFAULT 0,
    call_status TEXT NOT NULL DEFAULT 'none',
    calls INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scan_id TEXT NOT NULL,
    mc_number TEXT NOT NULL,
    status TEXT NOT NULL,
    entity TEXT,
    power_units TEXT,
    cargo TEXT,
    checked_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS check_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mc_number TEXT NOT NULL,
    authority_status TEXT,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS error_mcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scan_id TEXT,
    source TEXT NOT NULL DEFAULT 'scan',
    mc_number TEXT NOT NULL,
    error TEXT,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mc_number TEXT,
    title TEXT,
    text TEXT,
    tags TEXT,
    priority TEXT NOT NULL DEFAULT 'Normal',
    reminder_date TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS motus_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    label TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    checked INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY,
    default_start_mc INTEGER NOT NULL DEFAULT 1630000,
    default_end_mc INTEGER NOT NULL DEFAULT 1650000,
    cargo_general INTEGER NOT NULL DEFAULT 1,
    cargo_fresh INTEGER NOT NULL DEFAULT 1,
    cargo_reefer INTEGER NOT NULL DEFAULT 1,
    theme TEXT NOT NULL DEFAULT 'Light',
    layout TEXT NOT NULL DEFAULT 'Comfortable',
    sidebar TEXT NOT NULL DEFAULT 'Expanded',
    notify_email INTEGER NOT NULL DEFAULT 1,
    notify_scan INTEGER NOT NULL DEFAULT 1,
    notify_qualified INTEGER NOT NULL DEFAULT 1,
    notify_weekly INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_carriers_user_mc ON carriers(user_id, mc_number);
CREATE INDEX IF NOT EXISTS idx_carriers_user_source ON carriers(user_id, source);
CREATE INDEX IF NOT EXISTS idx_scan_log_user_scan ON scan_log(user_id, scan_id);
CREATE INDEX IF NOT EXISTS idx_check_hist_user_mc ON check_history(user_id, mc_number);
CREATE INDEX IF NOT EXISTS idx_scans_user ON scans(user_id);
-- Audit fix 9: rate-limit counters live in SQLite (not per-process
-- memory) so the 2 gunicorn workers share one count. One row per
-- attempt; old rows are pruned on every check.
CREATE TABLE IF NOT EXISTS rate_limit_events (
    key TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_key_ts ON rate_limit_events(key, ts);
-- Audit fix 9: failed reset-code guesses, shared across workers.
CREATE TABLE IF NOT EXISTS reset_failures (
    email TEXT PRIMARY KEY,
    fails INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    # Audit fix 5: a 30 s busy timeout plus WAL mode, so scan writes from
    # one worker do not fail outright when another worker writes at the
    # same moment (the old defaults were 5 s and rollback-journal).
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass  # e.g. read-only or in-memory DBs — journal mode is best-effort
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Migration for databases created before session_version existed.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "session_version" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN"
                         " session_version INTEGER NOT NULL DEFAULT 0")
        # Migration for opt-in TOTP 2FA (admin only): NULL = 2FA off.
        # The secret is stored only after the admin confirms setup with
        # a code; an unconfirmed secret never reaches the database.
        if "totp_secret" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        conn.commit()
    finally:
        conn.close()
