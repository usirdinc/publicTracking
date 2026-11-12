"""Deliver case changes through the account's selected channels."""
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import logging
from cryptography.fernet import InvalidToken

from app import config, db, mailer, security, sms, phone_verification

log = logging.getLogger("notifications")


def contact(cn, user):
    row = cn.execute("SELECT contact_enc FROM account_profiles WHERE user_id=?", (user["id"],)).fetchone()
    if row:
        return json.loads(security.decrypt(row["contact_enc"]))
    phones = set()
    for case in cn.execute("SELECT client_details_enc FROM cases WHERE user_id=?", (user["id"],)):
        if case["client_details_enc"]:
            try:
                legacy = json.loads(security.decrypt(case["client_details_enc"]))
                if legacy.get("phone"):
                    phones.add(security.normalize_contact_phone(legacy["phone"]))
            except (InvalidToken, ValueError, TypeError):
                continue
    return {"email": user["email"], "phone": next(iter(phones)) if len(phones) == 1 else "",
            "email_enabled": True, "sms_enabled": True}


def send_update(case, subject, body, kind, event_id=None, retry_failed=False):
    with closing(db.connect()) as cn:
        user = cn.execute("SELECT * FROM users WHERE id=?", (case["user_id"],)).fetchone()
        current = cn.execute("SELECT checked_at, uscis_checked_at, eoir_checked_at FROM cases WHERE id=?", (case["id"],)).fetchone()
        if not user or not current:
            return
        if kind == "aaf_reminder" and not (db.is_paid(user) or user["email"] in config.ADMIN_EMAILS):
            return
        try:
            profile = contact(cn, user)
        except Exception:
            log.error("Case %s: saved notification contacts could not be read", case["id"])
            return
        checked = event_id if event_id is not None else current["checked_at" if kind.startswith("aaf") else "uscis_checked_at"]
        event = hashlib.sha256(f"{case['id']}|{kind}|{checked}|{subject}".encode()).hexdigest()
        channels = []
        if profile.get("email_enabled", True):
            channels.append(("email", profile.get("email") or user["email"]))
        phone = profile.get("phone", "")
        if (profile.get("sms_enabled", True) and profile.get("sms_consented_at")
                and profile.get("sms_consent_phone") == phone and sms.configured()
                and phone_verification.is_verified(cn, user["id"], phone)):
            channels.append(("sms", phone))
        for channel, destination in channels:
            # A durable claim prevents duplicate sends after retries or concurrent workers.
            with db.tx(cn):
                if kind == "aaf_reminder":
                    recent = cn.execute(
                        "SELECT 1 FROM notification_deliveries WHERE case_id=? AND channel=? "
                        "AND kind IN ('aaf','aaf_reminder') AND state IN ('sending','accepted','uncertain') "
                        "AND created_at>? LIMIT 1",
                        (case["id"], channel, db.iso(db.now() - timedelta(hours=24)))).fetchone()
                    if recent:
                        continue
                claim = cn.execute(
                    "INSERT OR IGNORE INTO notification_deliveries "
                    "(event_key, channel, kind, user_id, case_id, state, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'sending', ?)",
                    (event, channel, kind, user["id"], case["id"], db.iso(db.now())))
                if not claim.rowcount and retry_failed:
                    claim=cn.execute("UPDATE notification_deliveries SET state='sending',created_at=? WHERE event_key=? AND channel=? AND state='failed'",
                        (db.iso(db.now()),event,channel))
            if not claim.rowcount:
                continue
            try:
                if channel == "email":
                    accepted = mailer.send(destination, subject, body)
                else:
                    if kind == "aaf_reminder":
                        text = ("USIRD Case Tracker reminder: USCIS still shows your Annual Asylum Fee as due. "
                                f"View details: {config.BASE_URL}/app Reply STOP to opt out.")
                    else:
                        text = (f"USIRD Case Tracker: Your {kind.upper()} status has changed. "
                                f"Sign in to view your update: {config.BASE_URL}/app "
                                "Reply STOP to opt out.")
                    accepted = sms.send(destination, text)
                state = "accepted" if accepted else "failed"
            except sms.DeliveryUncertain:
                state = "uncertain"
            except Exception:
                state = "failed"
            cn.execute("UPDATE notification_deliveries SET state=? WHERE event_key=? AND channel=?",
                       (state, event, channel))
            if state != "accepted":
                log.warning("Case %s %s notification %s", case["id"], channel, state)
