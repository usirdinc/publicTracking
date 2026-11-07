import json

from app import db, notifications, security, sms
from test_app import CLIENT_DETAILS, client, csrf, make_paid, signup


def test_channels_use_profile_contacts_once_and_respect_consent(client, monkeypatch):
    signup(client)
    make_paid("a@example.com")
    cn = db.connect()
    user = cn.execute("SELECT * FROM users").fetchone()
    cn.execute("INSERT OR REPLACE INTO account_profiles (user_id, contact_enc) VALUES (?,?)",
               (user["id"], security.encrypt(json.dumps({
                   "email": "contact@example.com", "phone": "+12025550142",
                   "email_enabled": True, "sms_enabled": True,
                   "sms_consent_phone": "+12025550142", "sms_consented_at": db.iso(db.now())}))))
    client.post("/cases", data={**CLIENT_DETAILS, "a_numbers": "A123456789",
                                "receipt": "ZAR1234567890", "csrf": csrf(client, "/cases/new")})
    case = cn.execute("SELECT * FROM cases").fetchone()
    from app import phone_verification
    cn.execute("UPDATE phone_verifications SET verified_phone_key=?, verified_at=? WHERE user_id=?",
               (phone_verification.digest("phone:+12025550142"), db.iso(db.now()), user["id"]))
    emails, texts = [], []
    monkeypatch.setattr(notifications.mailer, "send", lambda *args: emails.append(args) or True)
    monkeypatch.setattr(sms, "configured", lambda: True)
    monkeypatch.setattr(sms, "send", lambda *args: texts.append(args) or True)
    for _ in range(2):
        notifications.send_update(case, "An update", "Details", "aaf")
    assert len(emails) == len(texts) == 1
    assert emails[0][0] == "contact@example.com"
    assert texts[0][0] == "+12025550142"
    assert "STOP" in texts[0][1] and "A123456789" not in texts[0][1]
    assert [row["state"] for row in cn.execute("SELECT state FROM notification_deliveries")] == ["accepted", "accepted"]
    cn.execute("UPDATE account_profiles SET contact_enc=?",
               (security.encrypt(json.dumps({"phone": "+12025550142", "email_enabled": False,
                                             "sms_enabled": True})),))
    notifications.send_update(case, "A different update", "Details", "aaf")
    assert len(emails) == len(texts) == 1


def test_sms_requires_consent_for_new_phone_and_can_be_disabled(client):
    signup(client)
    token = csrf(client, "/account/settings")
    payload = {"csrf": token, "email": "contact@example.com", "phone": "2025550142",
               "email_enabled": "1", "sms_enabled": "1"}
    assert client.post("/account/settings", data=payload).status_code == 422
    assert "Profile settings saved" in client.post("/account/settings", data={**payload, "sms_consent": "1"}).text
    assert client.post("/account/settings", data={**payload, "phone": "2025550143"}).status_code == 422
    assert "Profile settings saved" in client.post("/account/settings", data={**payload, "sms_enabled": ""}).text
