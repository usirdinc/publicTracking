"""Background Google Cloud Translation queue. Never call Google while rendering a page."""
import hashlib
import html
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.fernet import InvalidToken
from app import config, security

API_URL = "https://translation.googleapis.com/language/translate/v2"
TOKEN = re.compile(r"USIRDPROTECTEDTOKEN[0-9]+X")
PRIVATE = re.compile(r"https?://[^\s<>]+|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\b[A-Za-z]{3}[ -]?[0-9]{10}\b|\bA[ -]?[0-9]{2,3}[ -]?[0-9]{3}[ -]?[0-9]{3}\b|(?<![0-9])[0-9]{8,}(?![0-9])", re.I)


def configured():
    return bool(config.USCIS_AUTO_TRANSLATE_ENABLED and config.GOOGLE_TRANSLATE_API_KEY)


@dataclass(frozen=True)
class Protected:
    text: str
    values: tuple

    def restore(self, translated):
        expected = [f"USIRDPROTECTEDTOKEN{i}X" for i in range(len(self.values))]
        if sorted(TOKEN.findall(translated)) != sorted(expected):
            raise ValueError("Protected translation tokens changed")
        for i, value in enumerate(self.values):
            translated = translated.replace(f"USIRDPROTECTEDTOKEN{i}X", value)
        return translated


def protect(text):
    if TOKEN.search(text):
        raise ValueError("Reserved translation token in input")
    values = []
    def replace(match):
        token = f"USIRDPROTECTEDTOKEN{len(values)}X"
        values.append(match[0])
        return token
    return Protected(PRIVATE.sub(replace, text), tuple(values))


def cache_key(template):
    return hashlib.sha256(("google-nmt-en-my-v1\0" + template).encode()).hexdigest()


def connect():
    path = Path(config.USCIS_TRANSLATION_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        pass
    cn = sqlite3.connect(str(path), timeout=2, isolation_level=None)
    cn.row_factory = sqlite3.Row
    cn.execute("PRAGMA busy_timeout=2000")
    cn.executescript("""
    CREATE TABLE IF NOT EXISTS translations (
      key TEXT PRIMARY KEY, source_enc TEXT NOT NULL, result_enc TEXT,
      state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
      retry_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, characters INTEGER NOT NULL DEFAULT 0);
    """)
    return cn


def lookup(text):
    """Return (ready|pending|unavailable, restored translation); never perform network I/O."""
    if not configured() or not text.strip() or len(text) > 4000:
        return "unavailable", None
    try:
        protected = protect(text)
        key = cache_key(protected.text)
        with closing(connect()) as cn:
            cn.execute("BEGIN IMMEDIATE")
            row = cn.execute("SELECT * FROM translations WHERE key=?", (key,)).fetchone()
            if row and row["state"] == "ready":
                return "ready", protected.restore(security.decrypt(row["result_enc"]))
            if row:
                return ("unavailable" if row["state"] == "failed" or row["retry_at"] > time.time()+300 else "pending"), None
            count = cn.execute("SELECT COUNT(*) FROM translations WHERE state IN ('queued','running')").fetchone()[0]
            if count >= 2000:
                return "unavailable", None
            cn.execute("INSERT OR IGNORE INTO translations(key,source_enc,updated_at) VALUES(?,?,?)",
                       (key, security.encrypt(protected.text), time.time()))
            cn.execute("COMMIT")
        return "pending", None
    except (sqlite3.Error, OSError, InvalidToken, ValueError, RuntimeError):
        return "unavailable", None


def google_translate(text, client=None):
    if not configured():
        raise ValueError("Translation is disabled")
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        response = client.post(API_URL, headers={"X-goog-api-key": config.GOOGLE_TRANSLATE_API_KEY},
                               json={"q": text, "source": "en", "target": "my", "format": "text", "model": "nmt"})
        if response.status_code != 200:
            raise ValueError("Translation provider rejected request")
        rows = response.json().get("data", {}).get("translations", [])
        translated = html.unescape(rows[0]["translatedText"]) if len(rows) == 1 else ""
        if not isinstance(translated, str) or not translated.strip() or len(translated) > 20000:
            raise ValueError("Invalid translation result")
        # Burmese is required; an English error or echoed input is not a translation.
        if not re.search(r"[\u1000-\u109f]", translated):
            raise ValueError("Translation result is not Burmese")
        if sorted(TOKEN.findall(text)) != sorted(TOKEN.findall(translated)):
            raise ValueError("Translation provider changed protected tokens")
        return translated
    except (httpx.HTTPError, KeyError, TypeError, IndexError):
        raise ValueError("Translation request failed") from None
    finally:
        if own:
            client.close()


def process_one(client=None):
    if not configured():
        return "disabled"
    now = time.time()
    with closing(connect()) as cn:
        cn.execute("BEGIN IMMEDIATE")
        try:
            # A killed worker can safely release a lease after five minutes.
            cn.execute("UPDATE translations SET state='queued' WHERE state='running' AND updated_at<?", (now-300,))
            row = cn.execute("SELECT * FROM translations WHERE state='queued' AND retry_at<=? ORDER BY updated_at LIMIT 1", (now,)).fetchone()
            if not row:
                cn.execute("COMMIT")
                return "idle"
            try:
                text = security.decrypt(row["source_enc"])
            except (InvalidToken, ValueError):
                cn.execute("UPDATE translations SET state='failed' WHERE key=?", (row["key"],))
                cn.execute("COMMIT")
                return "failed"
            day = datetime.now(timezone.utc).date().isoformat()
            cn.execute("INSERT OR IGNORE INTO usage(day) VALUES(?)", (day,))
            used = cn.execute("SELECT characters FROM usage WHERE day=?", (day,)).fetchone()[0]
            if used + len(text) > config.USCIS_TRANSLATION_DAILY_CHAR_LIMIT:
                tomorrow = (int(now // 86400) + 1) * 86400
                cn.execute("UPDATE translations SET retry_at=? WHERE key=?", (tomorrow,row["key"]))
                cn.execute("COMMIT")
                return "budget"
            cn.execute("UPDATE usage SET characters=characters+? WHERE day=?", (len(text), day))
            cn.execute("UPDATE translations SET state='running',attempts=attempts+1,updated_at=? WHERE key=?", (now,row["key"]))
            cn.execute("COMMIT")
        except Exception:
            cn.execute("ROLLBACK")
            raise
        try:
            result = google_translate(text, client)
            cn.execute("UPDATE translations SET state='ready',result_enc=?,updated_at=? WHERE key=?",
                       (security.encrypt(result),time.time(),row["key"]))
            return "ready"
        except ValueError:
            state = "failed" if row["attempts"] >= 2 else "queued"
            cn.execute("UPDATE translations SET state=?,retry_at=?,updated_at=? WHERE key=?",
                       (state,time.time()+min(3600,60*(2**row["attempts"])),time.time(),row["key"]))
            return state
