import base64, hashlib, hmac, json, re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, security, square
from app.main import app
from worker import run as worker


@pytest.fixture()
def client():
    cn = db.connect(); db.init(cn)
    for t in ("jobs", "checks", "cases", "users", "webhook_events", "settings"):
        cn.execute(f"DELETE FROM {t}")
    import app.main as m
    m._attempts.clear()
    return TestClient(app)


def csrf(c, path="/signup"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def signup(c, email="a@example.com", pw="correct horse battery"):
    return c.post("/signup", data={"email": email, "password": pw, "agree": "1", "csrf": csrf(c)},
                  follow_redirects=False)


def make_paid(email):
    cn = db.connect()
    cn.execute("UPDATE users SET paid_until=? WHERE email=?", (db.in_(days=30), email))


def test_signup_and_unpaid_cannot_add(client):
    assert signup(client).status_code == 303
    r = client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890",
                                    "csrf": csrf(client, "/app")})
    assert "subscribe first" in r.text.lower()


def test_csrf_required(client):
    signup(client)
    make_paid("a@example.com")
    r = client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": "x"})
    assert "session expired" in r.text.lower()


def test_add_case_encrypts_and_queues_instant(client):
    signup(client); make_paid("a@example.com")
    r = client.post("/cases", data={"a_numbers": "a123-456-789, 98765432", "receipt": "zar 1234567890",
                                    "label": "Fam", "csrf": csrf(client, "/app")})
    assert "Case added" in r.text
    cn = db.connect()
    c = cn.execute("SELECT * FROM cases").fetchone()
    assert "123456789" not in c["a_numbers_enc"]
    assert security.decrypt(c["a_numbers_enc"]) == "A123456789;A098765432"
    assert security.decrypt(c["receipt_enc"]) == "ZAR1234567890"
    j = cn.execute("SELECT * FROM jobs").fetchone()
    assert j["kind"] == "new" and j["priority"] == 0


def test_bad_receipt_rejected(client):
    signup(client); make_paid("a@example.com")
    r = client.post("/cases", data={"a_numbers": "A123456789", "receipt": "123", "csrf": csrf(client, "/app")})
    assert "Receipt numbers look like" in r.text


def test_other_user_cannot_touch_case(client):
    signup(client, "a@example.com"); make_paid("a@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": csrf(client, "/app")})
    cid = db.connect().execute("SELECT id FROM cases").fetchone()["id"]
    client.post("/logout")
    other = TestClient(app)
    signup(other, "b@example.com"); make_paid("b@example.com")
    other.post(f"/cases/{cid}/delete", data={"csrf": csrf(other, "/app")})
    assert db.connect().execute("SELECT COUNT(*) c FROM cases").fetchone()["c"] == 1
    assert "A-Number ending" not in other.get("/app").text


def test_instant_limit(client):
    signup(client); make_paid("a@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": csrf(client, "/app")})
    cn = db.connect(); cid = cn.execute("SELECT id FROM cases").fetchone()["id"]
    cn.execute("UPDATE jobs SET state='done'")
    t = csrf(client, "/app")
    assert "Checking now" in client.post(f"/cases/{cid}/check", data={"csrf": t}).text
    cn.execute("UPDATE jobs SET state='done'")
    assert "one instant check" in client.post(f"/cases/{cid}/check", data={"csrf": t}).text


def _sign(body: bytes):
    mac = hmac.new(b"sigkey", b"http://testserver/webhooks/square" + body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def test_square_webhook_activates_by_email(client, monkeypatch):
    signup(client)
    monkeypatch.setattr(square, "customer_email", lambda cid: "a@example.com")
    ev = {"event_id": "e1", "type": "invoice.payment_made",
          "data": {"object": {"invoice": {"primary_recipient": {"customer_id": "C1"}}}}}
    body = json.dumps(ev).encode()
    assert client.post("/webhooks/square", content=body, headers={"x-square-hmacsha256-signature": "bad"}).status_code == 403
    assert client.post("/webhooks/square", content=body, headers={"x-square-hmacsha256-signature": _sign(body)}).status_code == 200
    u = db.connect().execute("SELECT * FROM users").fetchone()
    assert db.is_paid(u) and u["square_customer_id"] == "C1"
    # duplicate event does not extend twice
    before = u["paid_until"]
    client.post("/webhooks/square", content=body, headers={"x-square-hmacsha256-signature": _sign(body)})
    assert db.connect().execute("SELECT paid_until FROM users").fetchone()["paid_until"] == before


def _setup_case(client):
    signup(client); make_paid("a@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": csrf(client, "/app")})
    return db.connect()


def test_worker_success_schedules_next(client, monkeypatch):
    cn = _setup_case(client)
    sent = []
    monkeypatch.setattr(worker.mailer, "send", lambda *a: sent.append(a))
    job = worker.claim(cn)
    assert worker.process(cn, job, lambda a, r: ("NOT_DUE", "", "", a[0])) == "NOT_DUE"
    c = cn.execute("SELECT * FROM cases").fetchone()
    assert c["status"] == "NOT_DUE"
    nxt = cn.execute("SELECT * FROM jobs WHERE state='queued'").fetchall()
    assert len(nxt) == 1 and nxt[0]["kind"] == "scheduled"
    assert not sent
    # status change -> email
    cn.execute("UPDATE jobs SET run_after=?", (db.iso(db.now()),))
    job = worker.claim(cn)
    worker.process(cn, job, lambda a, r: ("DUE", "100.00", "", a[0]))
    assert sent and "Fee due: $100.00" in sent[0][1]


def test_worker_block_pauses_and_requeues(client, monkeypatch):
    cn = _setup_case(client)
    monkeypatch.setattr(worker.mailer, "send", lambda *a: None)
    job = worker.claim(cn)
    assert worker.process(cn, job, lambda a, r: ("__BLOCKED__", "", "access denied", "")) == "__BLOCKED__"
    assert worker.paused(cn)
    j = cn.execute("SELECT * FROM jobs").fetchone()
    assert j["state"] == "queued" and worker.claim(cn) is None


def test_worker_error_retries_then_fails(client, monkeypatch):
    cn = _setup_case(client)
    err = lambda a, r: ("ERROR/incomplete_form", "", "", "")
    for _ in range(3):
        cn.execute("UPDATE jobs SET run_after=? WHERE state='queued'", (db.iso(db.now()),))
        job = worker.claim(cn)
        worker.process(cn, job, err)
    states = [r["state"] for r in cn.execute("SELECT state FROM jobs ORDER BY id")]
    assert states[0] == "failed" and "queued" in states  # retry tomorrow


def test_unpaid_scheduled_job_skipped(client):
    cn = _setup_case(client)
    cn.execute("UPDATE jobs SET kind='scheduled'")
    cn.execute("UPDATE users SET paid_until=?", (db.iso(db.now() - timedelta(days=1)),))
    job = worker.claim(cn)
    assert worker.process(cn, job, lambda a, r: 1 / 0) == "skipped"


def test_admin_requires_admin(client):
    signup(client)
    assert client.get("/admin", follow_redirects=False).status_code == 303
