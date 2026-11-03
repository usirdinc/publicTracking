"""SQLite storage. One file, WAL mode; the web app and the worker share it."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  paid_until TEXT,
  square_customer_id TEXT,
  subscription_status TEXT
);
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  a_numbers_enc TEXT NOT NULL,
  receipt_enc TEXT NOT NULL,
  a_last3 TEXT NOT NULL,
  receipt_last4 TEXT NOT NULL,
  status TEXT,
  amount TEXT,
  note TEXT,
  checked_at TEXT,
  next_check_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- instant | scheduled | retry
  priority INTEGER NOT NULL,        -- lower runs first
  run_after TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  result_status TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(state, priority, run_after);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  amount TEXT,
  note TEXT,
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY,
  type TEXT,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(s):
    return datetime.fromisoformat(s) if s else None


def in_(**kw) -> str:
    return iso(now() + timedelta(**kw))


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cn = sqlite3.connect(path, timeout=30, isolation_level=None)
    cn.row_factory = sqlite3.Row
    cn.execute("PRAGMA journal_mode=WAL")
    cn.execute("PRAGMA foreign_keys=ON")
    cn.execute("PRAGMA busy_timeout=30000")
    return cn


# Columns added after launch; init() adds any that are missing.
MIGRATIONS = {
    "cases": [
        ("uscis_status", "TEXT"), ("uscis_desc", "TEXT"), ("uscis_form", "TEXT"),
        ("uscis_modified", "TEXT"), ("uscis_checked_at", "TEXT"), ("uscis_next_at", "TEXT"),
        ("uscis_error", "TEXT"), ("uscis_history_enc", "TEXT"),
        ("nationality", "TEXT"), ("eoir_status", "TEXT"), ("eoir_fields", "TEXT"),
        ("eoir_checked_at", "TEXT"), ("eoir_next_at", "TEXT"),
    ],
    "jobs": [("progress", "INTEGER"), ("stage", "TEXT")],
}


def init(cn: sqlite3.Connection) -> None:
    cn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in cn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                try:
                    cn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                except sqlite3.OperationalError as e:  # another process added it first
                    if "duplicate column" not in str(e):
                        raise


@contextmanager
def tx(cn: sqlite3.Connection):
    cn.execute("BEGIN IMMEDIATE")
    try:
        yield cn
        cn.execute("COMMIT")
    except Exception:
        cn.execute("ROLLBACK")
        raise


def get_setting(cn, key, default=None):
    r = cn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(cn, key, value):
    cn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def is_paid(user_row) -> bool:
    pu = parse(user_row["paid_until"]) if user_row else None
    return bool(pu and pu > now())


def enqueue(cn, case_id: int, kind: str, run_after: str | None = None) -> int:
    pr = {"new": 0, "instant": 0, "retry": 5, "scheduled": 10}[kind]
    cur = cn.execute(
        "INSERT INTO jobs(case_id,kind,priority,run_after,created_at) VALUES(?,?,?,?,?)",
        (case_id, kind, pr, run_after or iso(now()), iso(now())))
    return cur.lastrowid
