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

def test_daily_due_reminders_once_per_day_in_both_channels_and_stop_when_not_due(client, monkeypatch):
    from datetime import timedelta
    from app import phone_verification
    from worker import run as worker

    signup(client)
    make_paid("a@example.com")
    cn = db.connect()
    user = cn.execute("SELECT * FROM users").fetchone()
    profile = {"email": "contact@example.com", "phone": "+12025550142",
               "email_enabled": True, "sms_enabled": True,
               "sms_consent_phone": "+12025550142", "sms_consented_at": db.iso(db.now())}
    cn.execute("UPDATE account_profiles SET contact_enc=?", (security.encrypt(json.dumps(profile)),))
    cn.execute("UPDATE phone_verifications SET verified_phone_key=?, verified_at=?",
               (phone_verification.digest("phone:+12025550142"), db.iso(db.now())))
    client.post("/cases", data={**CLIENT_DETAILS, "a_numbers": "A123456789",
                                "receipt": "ZAR1234567890", "csrf": csrf(client, "/cases/new")})
    case = cn.execute("SELECT * FROM cases").fetchone()
    clock = [db.now()]
    monkeypatch.setattr(db, "now", lambda: clock[0])
    emails, texts = [], []
    monkeypatch.setattr(notifications.mailer, "send", lambda *args: emails.append(args) or True)
    monkeypatch.setattr(sms, "send", lambda *args: texts.append(args) or True)

    def run_check(status):
        job = worker.claim(cn)
        assert job
        worker.process(cn, job, lambda numbers, receipt: (status, "105", "", numbers[0]))

    run_check("DUE")
    assert len(emails) == len(texts) == 1

    clock[0] += timedelta(hours=6)
    db.enqueue(cn, case["id"], "instant")
    run_check("DUE")
    assert len(emails) == len(texts) == 1

    clock[0] += timedelta(hours=18)
    db.enqueue(cn, case["id"], "instant")
    run_check("DUE")
    assert len(emails) == len(texts) == 2
    assert "reminder" in emails[-1][1].lower() and "reminder" in texts[-1][1].lower()

    db.enqueue(cn, case["id"], "instant")
    run_check("NOT_DUE")
    assert len(emails) == len(texts) == 3

    clock[0] += timedelta(hours=24)
    run_check("NOT_DUE")
    assert len(emails) == len(texts) == 3
