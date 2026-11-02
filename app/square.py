"""Square Subscriptions webhook: verify signature, match customer to user by email,
extend paid_until on each paid invoice."""
import base64
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx

from app import config, db

log = logging.getLogger("square")
SQUARE_VERSION = "2024-10-17"


def api_base() -> str:
    return ("https://connect.squareupsandbox.com" if config.SQUARE_ENV == "sandbox"
            else "https://connect.squareup.com")


def verify_signature(raw_body: bytes, signature: str, notification_url: str,
                     key: str | None = None) -> bool:
    key = key if key is not None else config.SQUARE_WEBHOOK_SIGNATURE_KEY
    if not key or not signature:
        return False
    mac = hmac.new(key.encode(), notification_url.encode() + raw_body, hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature)


def customer_email(customer_id: str) -> str | None:
    if not (config.SQUARE_ACCESS_TOKEN and customer_id):
        return None
    r = httpx.get(f"{api_base()}/v2/customers/{customer_id}", timeout=20, headers={
        "Authorization": f"Bearer {config.SQUARE_ACCESS_TOKEN}",
        "Square-Version": SQUARE_VERSION})
    if r.status_code != 200:
        log.error("customer lookup %s -> %s", customer_id, r.status_code)
        return None
    return ((r.json().get("customer") or {}).get("email_address") or "").strip().lower() or None


def _find_user(cn, customer_id, lookup):
    u = cn.execute("SELECT * FROM users WHERE square_customer_id=?", (customer_id,)).fetchone()
    if u:
        return u
    email = lookup(customer_id)
    if not email:
        return None
    u = cn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        cn.execute("UPDATE users SET square_customer_id=? WHERE id=?", (customer_id, u["id"]))
    return u


def handle_event(cn, event: dict, lookup=None) -> str:
    """Apply one webhook event. Returns a short outcome string (for logs/tests)."""
    lookup = lookup or customer_email
    eid = event.get("event_id")
    etype = event.get("type", "")
    if eid:
        try:
            cn.execute("INSERT INTO webhook_events(event_id,type,received_at) VALUES(?,?,?)",
                       (eid, etype, db.iso(db.now())))
        except Exception:
            return "duplicate"
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "invoice.payment_made":
        inv = obj.get("invoice") or {}
        cid = (inv.get("primary_recipient") or {}).get("customer_id")
        u = _find_user(cn, cid, lookup)
        if not u:
            log.warning("payment for unknown customer %s", cid)
            return "unmatched"
        base = max(db.now(), db.parse(u["paid_until"]) or db.now())
        until = base + timedelta(days=config.PAID_DAYS_PER_PAYMENT)
        cn.execute("UPDATE users SET paid_until=?, subscription_status='ACTIVE' WHERE id=?",
                   (db.iso(until), u["id"]))
        return f"paid:{u['id']}"

    if etype in ("subscription.created", "subscription.updated"):
        sub = obj.get("subscription") or {}
        u = _find_user(cn, sub.get("customer_id"), lookup)
        if not u:
            return "unmatched"
        cn.execute("UPDATE users SET subscription_status=? WHERE id=?", (sub.get("status"), u["id"]))
        return f"status:{u['id']}:{sub.get('status')}"

    return "ignored"


def parse_body(raw: bytes) -> dict:
    return json.loads(raw.decode() or "{}")
