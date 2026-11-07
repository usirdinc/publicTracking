"""Phone ownership checks with expiring codes and persistent send limits."""
import hashlib
import json
import hmac
import re
import secrets
from datetime import timedelta

from app import config, db, security, sms


def digest(value):
    return hmac.new(config.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def is_verified(cn, user_id, phone):
    if not phone:
        return False
    row = cn.execute("SELECT verified_phone_key, verified_at FROM phone_verifications WHERE user_id=?",
                     (user_id,)).fetchone()
    return bool(row and row["verified_at"] and row["verified_phone_key"] == digest("phone:" + phone))


def send_code(cn, user, phone, ip):
    phone = security.normalize_contact_phone(phone)
    if not sms.configured():
        raise ValueError("Text verification is temporarily unavailable. Please try again shortly.")
    now = db.now()
    phone_key, ip_key = digest("phone:" + phone), digest("ip:" + ip)
    code = f"{secrets.randbelow(1000000):06d}"
    challenge = secrets.token_hex(16)
    code_hash = digest(f"{user['id']}:{phone}:{challenge}:{code}")
    with db.tx(cn):
        previous = cn.execute("SELECT last_sent_at FROM phone_verifications WHERE user_id=?", (user["id"],)).fetchone()
        if previous and previous["last_sent_at"] and db.parse(previous["last_sent_at"]) > now - timedelta(seconds=60):
            raise ValueError("Wait one minute before requesting another code.")
        since = db.iso(now - timedelta(days=1))
        counts = cn.execute(
            "SELECT COALESCE(SUM(user_id=?),0) AS per_user, COALESCE(SUM(phone_key=?),0) AS per_phone, "
            "COALESCE(SUM(ip_key=?),0) AS per_ip FROM verification_sends WHERE created_at>?",
            (user["id"], phone_key, ip_key, since)).fetchone()
        if counts["per_user"] >= 5 or counts["per_phone"] >= 5 or counts["per_ip"] >= 20:
            raise ValueError("The daily code limit has been reached. Please try again tomorrow.")
        cn.execute(
            "INSERT INTO phone_verifications (user_id,phone_enc,code_hash,challenge,expires_at,last_sent_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET phone_enc=excluded.phone_enc, "
            "code_hash=excluded.code_hash,challenge=excluded.challenge,expires_at=excluded.expires_at,"
            "last_sent_at=excluded.last_sent_at,attempts=0",
            (user["id"], security.encrypt(phone), code_hash, challenge, db.iso(now + timedelta(minutes=10)), db.iso(now)))
        cn.execute("INSERT INTO verification_sends (user_id,phone_key,ip_key,created_at) VALUES (?,?,?,?)",
                   (user["id"], phone_key, ip_key, db.iso(now)))
        row = cn.execute("SELECT contact_enc FROM account_profiles WHERE user_id=?", (user["id"],)).fetchone()
        profile = json.loads(security.decrypt(row["contact_enc"])) if row else {
            "email": user["email"], "email_enabled": True, "sms_enabled": True}
        if profile.get("phone") != phone:
            profile.pop("sms_consent_phone", None)
            profile.pop("sms_consented_at", None)
        profile["phone"] = phone
        cn.execute("INSERT INTO account_profiles (user_id,contact_enc) VALUES (?,?) "
                   "ON CONFLICT(user_id) DO UPDATE SET contact_enc=excluded.contact_enc",
                   (user["id"], security.encrypt(json.dumps(profile))))
    try:
        accepted = sms.send(phone, f"Your USIRD Case Tracker verification code is {code}. "
                            "It expires in 10 minutes. Do not share this code.")
    except sms.DeliveryUncertain:
        return "Delivery is taking longer to confirm. If the code arrives, enter it below. Wait a minute before requesting another."
    except Exception:
        accepted = False
    if not accepted:
        cn.execute("UPDATE phone_verifications SET code_hash=NULL WHERE user_id=? AND challenge=?",
                   (user["id"], challenge))
        raise ValueError("We could not send the text. Check your number and try again in a minute.")
    return "Verification code sent. It expires in 10 minutes."


def confirm_code(cn, user, phone, code):
    with db.tx(cn):
        row = cn.execute("SELECT * FROM phone_verifications WHERE user_id=?", (user["id"],)).fetchone()
        if (not row or not row["code_hash"] or not row["expires_at"]
                or db.parse(row["expires_at"]) <= db.now() or row["attempts"] >= 5):
            return False
        target = security.decrypt(row["phone_enc"])
        cn.execute("UPDATE phone_verifications SET attempts=attempts+1 WHERE user_id=?", (user["id"],))
        candidate = digest(f"{user['id']}:{target}:{row['challenge']}:{code}")
        if phone != target or not re.fullmatch(r"[0-9]{6}", code) or not hmac.compare_digest(candidate, row["code_hash"]):
            return False
        cn.execute("UPDATE phone_verifications SET verified_phone_key=?,verified_at=?,code_hash=NULL WHERE user_id=?",
                   (digest("phone:" + target), db.iso(db.now()), user["id"]))
        cn.execute("UPDATE users SET phone_verification_required=0 WHERE id=?", (user["id"],))
    return True
