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


def test_status_not_busy_after_check(client, monkeypatch):
    cn = _setup_case(client)
    monkeypatch.setattr(worker.mailer, "send", lambda *a: None)
    assert client.get("/cases/status").json()["cases"][0]["busy"] is True
    worker.process(cn, worker.claim(cn), lambda a, r: ("NOT_DUE", "", "", a[0]))
    c = client.get("/cases/status").json()["cases"][0]
    assert c["busy"] is False and c["text"] == "Not due right now"
    # the queued monthly job must not block "Check now"
    r = client.post(f"/cases/{c['id']}/check", data={"csrf": csrf(client, "/app")})
    assert "Checking now" in r.text


def test_eoir_staff_queue(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "EOIR_ENABLED", True)
    import app.main as m
    m.templates.env.globals["eoir_enabled"] = True
    import app.mailer as mailer
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subj, body: sent.append(subj))
    signup(client, "admin@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890",
                                "nationality": "Burma", "csrf": csrf(client, "/app")})
    q = client.get("/admin/eoir")
    assert "A123456789" in q.text and q.headers["cache-control"] == "no-store"
    cid = db.connect().execute("SELECT id FROM cases").fetchone()["id"]
    tok = csrf(client, "/admin/eoir")
    page = ("Name: DOE, JOHN | A-Number: 123-456-789\nYour next MASTER hearing is IN PERSON on "
            "March 3, 2027 at 8:30 AM\nJUDGE\nSmith, Jane\nThis case is pending")
    r = client.post(f"/admin/eoir/{cid}", data={"page_text": page, "csrf": tok})
    assert "Next hearing March 3, 2027" in r.text
    c = db.connect().execute("SELECT * FROM cases").fetchone()
    assert c["eoir_status"] == "CASE_FOUND" and "DOE" not in c["eoir_fields"] and c["eoir_next_at"]
    assert sent and "Immigration court" in sent[0]
    assert "A123456789" not in client.get("/admin/eoir").text   # not due again for 30 days
    assert "Next hearing March 3, 2027" in client.get("/app").text


def test_eoir_queue_admin_only(client):
    signup(client)
    assert client.get("/admin/eoir", follow_redirects=False).status_code == 303


def test_eoir_hidden_when_disabled(client):
    import app.main as m
    m.templates.env.globals["eoir_enabled"] = False
    signup(client, "admin@example.com")
    assert "nationality" not in client.get("/app").text.lower()
    assert "/admin/eoir" not in client.get("/admin").text
    assert client.get("/admin/eoir", follow_redirects=False).status_code == 303


def test_status_reports_progress(client):
    signup(client); make_paid("a@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": csrf(client, "/app")})
    c = client.get("/cases/status").json()["cases"][0]
    assert c["busy"] and c["progress"] == 5 and c["stage"].startswith("Waiting in line")
    cn = db.connect()
    cn.execute("UPDATE jobs SET state='running', progress=45, stage='Entering A-Number'")
    c = client.get("/cases/status").json()["cases"][0]
    assert c["progress"] == 45 and c["stage"] == "Entering A-Number" and c["text"] == "Checking… 45%"


def test_aaf_rechecks_daily(client):
    signup(client); make_paid("a@example.com")
    client.post("/cases", data={"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": csrf(client, "/app")})
    cn = db.connect()
    worker.process(cn, worker.claim(cn), lambda a, r: ("NOT_DUE", "", "", a[0]))
    nxt = db.parse(cn.execute("SELECT next_check_at FROM cases").fetchone()["next_check_at"])
    assert timedelta(hours=23) < nxt - db.now() <= timedelta(hours=24)


def test_redesign_keeps_case_privacy_and_post_contracts(client):
    cn = _setup_case(client)
    cid = cn.execute("SELECT id FROM cases").fetchone()["id"]
    cn.execute("UPDATE cases SET label=?, a_numbers_enc=?, receipt_enc=?",
               ('<script>alert("example")</script>', security.encrypt("A987654321"), security.encrypt("ZLA9876543210")))
    response = client.get("/app")
    html = response.text
    assert response.status_code == 200
    assert "A987654321" not in html and "ZLA9876543210" not in html
    assert "&lt;script&gt;" in html and '<script>alert("example")</script>' not in html
    assert f'data-case="{cid}"' in html and 'data-stepper' in html
    for action in ("/cases", f"/cases/{cid}/check", f"/cases/{cid}/delete", "/account/delete"):
        form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>.*?</form>', html, re.S)
        assert form, action
        assert 'method="post"' in form.group() and 'name="csrf"' in form.group()
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert not re.search(r'\s(?:style|onclick|onsubmit)=', html, re.I)


def test_redesign_keeps_public_pages_self_contained_and_legal_acceptance_explicit(client):
    for path in ("/", "/login", "/signup", "/terms", "/privacy"):
        response = client.get(path)
        assert response.status_code == 200
        html = response.text
        assert '<html lang="en"' in html
        assert not re.search(r'<(?:script|link)\b[^>]*(?:src|href)="https?://', html)
        assert not re.search(r'\s(?:style|onclick|onsubmit)=', html, re.I)
    signup_html = client.get("/signup").text
    consent = re.search(r'<input\b[^>]*name="agree"[^>]*>', signup_html).group()
    assert "required" in consent and "checked" not in consent
    assert 'autocomplete="new-password"' in signup_html
    assert 'autocomplete="current-password"' in client.get("/login").text


def test_redesigned_login_handles_error_then_authenticated_redirect(client):
    signup(client, "login@example.com"); make_paid("login@example.com")
    client.post("/logout")
    bad = client.post("/login", data={"email": "login@example.com", "password": "incorrect example", "csrf": csrf(client, "/login")})
    assert "Email or password is not correct" in bad.text
    assert "data-dismiss-notice" in bad.text
    good = client.post("/login", data={"email": "login@example.com", "password": "correct horse battery", "csrf": csrf(client, "/login")}, follow_redirects=False)
    assert good.status_code == 303 and good.headers["location"] == "/app"
    assert "login@example.com" in client.get("/app").text


def test_signup_goes_to_subscribe_and_app_is_locked_until_paid(client):
    r = signup(client)
    assert r.headers["location"] == "/subscribe"
    page = client.get("/subscribe")
    assert "a@example.com" in page.text and "Start your subscription" in page.text
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/subscribe"
    assert client.get("/account/paid").json() == {"paid": False}
    make_paid("a@example.com")
    assert client.get("/account/paid").json() == {"paid": True}
    r = client.get("/subscribe", follow_redirects=False)
    assert r.headers["location"] == "/app"
    assert client.get("/app").status_code == 200


def test_login_unpaid_goes_to_subscribe(client):
    signup(client)
    client.post("/logout", data={})
    r = client.post("/login", data={"email": "a@example.com", "password": "correct horse battery",
                                    "csrf": csrf(client, "/login")}, follow_redirects=False)
    assert r.headers["location"] == "/subscribe"


def test_square_confirmation_controls_registration_return(client, monkeypatch):
    signup(client)
    assert client.get('/app', follow_redirects=False).headers['location'] == '/subscribe'
    monkeypatch.setattr(square, 'customer_email', lambda cid: 'a@example.com')
    subscription = {'event_id':'signup-sub', 'type':'subscription.created',
                    'data':{'object':{'subscription':{'customer_id':'C-signup','status':'ACTIVE'}}}}
    body = json.dumps(subscription).encode()
    client.post('/webhooks/square', content=body, headers={'x-square-hmacsha256-signature':_sign(body)})
    assert client.get('/account/paid').json() == {'paid':False}
    event = {'event_id':'signup-payment', 'type':'invoice.payment_made',
             'data':{'object':{'invoice':{'primary_recipient':{'customer_id':'C-signup'}}}}}
    body = json.dumps(event).encode()
    assert client.post('/webhooks/square', content=body, headers={'x-square-hmacsha256-signature':'invalid'}).status_code == 403
    assert client.get('/account/paid').json() == {'paid':False}
    client.post('/webhooks/square', content=body, headers={'x-square-hmacsha256-signature':_sign(body)})
    response = client.get('/account/paid')
    assert response.json() == {'paid':True} and 'no-store' in response.headers['cache-control']
    assert client.get('/subscribe', follow_redirects=False).headers['location'] == '/app'
    assert client.get('/app').status_code == 200
