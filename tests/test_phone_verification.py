import re
from datetime import timedelta

from app import db, phone_verification, sms
from test_app import client, csrf, signup, make_paid


def test_new_signup_requires_sms_code_before_checkout(client, monkeypatch):
    sent = []
    monkeypatch.setattr(sms, "send", lambda phone, text: sent.append((phone, text)) or True)
    response = client.post("/signup", data={"email": "new@example.com", "password": "a strong password",
                                            "phone": "2025550142", "agree": "1", "csrf": csrf(client)},
                           follow_redirects=False)
    assert response.headers["location"] == "/account/verify-phone"
    assert sent[0][0] == "+12025550142"
    code = re.search(r"code is ([0-9]{6})", sent[0][1]).group(1)
    cn = db.connect()
    row = cn.execute("SELECT * FROM phone_verifications").fetchone()
    assert code not in row["code_hash"] and "+12025550142" not in row["phone_enc"]
    assert client.get("/subscribe", follow_redirects=False).headers["location"] == "/account/verify-phone"
    assert client.post("/billing/subscribe", data={"csrf": csrf(client, "/account/verify-phone")}).status_code == 403
    assert client.post("/account/verify-phone", data={"action": "confirm", "code": code, "csrf": "bad"}).status_code == 403
    response = client.post("/account/verify-phone", data={"action": "confirm", "code": code,
                                                        "csrf": csrf(client, "/account/verify-phone")},
                           follow_redirects=False)
    assert response.headers["location"] == "/subscribe"
    user = cn.execute("SELECT * FROM users").fetchone()
    assert not user["phone_verification_required"]
    assert phone_verification.is_verified(cn, user["id"], "+12025550142")
    assert not phone_verification.is_verified(cn, user["id"], "+12025550143")
    assert not phone_verification.confirm_code(cn, user, "+12025550142", code)


def test_existing_account_gets_verify_prompt_and_cannot_guess_unlimited(client, monkeypatch):
    signup(client)
    make_paid("a@example.com")
    assert "Confirm your phone number" in client.get("/app").text
    cn = db.connect()
    user = cn.execute("SELECT * FROM users").fetchone()
    cn.execute("UPDATE phone_verifications SET last_sent_at=?", (db.iso(db.now() - timedelta(minutes=2)),))
    sent = []
    monkeypatch.setattr(sms, "send", lambda phone, text: sent.append(text) or True)
    phone_verification.send_code(cn, user, "+12025550142", "127.0.0.1")
    code = re.search(r"code is ([0-9]{6})", sent[0]).group(1)
    wrong = "999999" if code != "999999" else "000000"
    for _ in range(5):
        assert not phone_verification.confirm_code(cn, user, "+12025550142", wrong)
    assert not phone_verification.confirm_code(cn, user, "+12025550142", code)
    assert client.get("/app").status_code == 200


def test_code_send_rate_limit_expiry_and_changed_phone(client, monkeypatch):
    signup(client)
    cn = db.connect()
    user = cn.execute("SELECT * FROM users").fetchone()
    token = csrf(client, "/account/verify-phone")
    payload = {"action": "send", "phone": "2025550142", "csrf": token}
    assert "Wait one minute" in client.post("/account/verify-phone", data=payload).text
    cn.execute("UPDATE phone_verifications SET last_sent_at=?", (db.iso(db.now() - timedelta(minutes=2)),))
    sent = []
    monkeypatch.setattr(sms, "send", lambda phone, text: sent.append(text) or True)
    phone_verification.send_code(cn, user, "+12025550142", "127.0.0.1")
    code = re.search(r"code is ([0-9]{6})", sent[0]).group(1)
    assert not phone_verification.confirm_code(cn, user, "+12025550143", code)
    cn.execute("UPDATE phone_verifications SET expires_at=?", (db.iso(db.now() - timedelta(seconds=1)),))
    assert not phone_verification.confirm_code(cn, user, "+12025550142", code)
    for _ in range(5):
        cn.execute("INSERT INTO verification_sends(user_id,phone_key,ip_key,created_at) VALUES(?,?,?,?)",
                   (user["id"], phone_verification.digest("phone:+12025550142"), "ip", db.iso(db.now())))
    cn.execute("UPDATE phone_verifications SET last_sent_at=?", (db.iso(db.now() - timedelta(minutes=2)),))
    assert "daily code limit" in client.post("/account/verify-phone", data=payload).text
