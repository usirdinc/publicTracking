import json

import httpx
import pytest

from app import db, security
from tracker.uscis_api import RateLimited, UscisClient
from worker import uscis_run


def make_client(handler):
    return UscisClient("id", "secret", "demo", http=httpx.Client(transport=httpx.MockTransport(handler)))


def ok_body(text="Case Was Received", desc="We received your Form I-589."):
    return {"case_status": {"receiptNumber": "ZAR1234567890", "formType": "I-589",
                            "submittedDate": "01-02-2024", "modifiedDate": "2026-10-01",
                            "current_case_status_text_en": text,
                            "current_case_status_desc_en": desc, "hist_case_status": None}}


def test_token_has_demo_id_and_lookup_parses():
    seen = []

    def h(req):
        seen.append(req)
        if req.url.path.endswith("/oauth/accesstoken"):
            assert req.headers["demo_id"] == "demo"
            assert b"grant_type=client_credentials" in req.content
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 1799})
        assert req.headers["authorization"] == "Bearer T1"
        assert req.url.path == "/case-status/ZAR1234567890"
        return httpx.Response(200, json=ok_body())
    r = make_client(h).case_status("zar1234567890")
    assert r.ok and r.status == "Case Was Received" and r.form == "I-589"
    assert "demo_id" not in seen[-1].headers


def test_401_refreshes_token_once():
    tokens = iter(["OLD", "NEW"])
    calls = []

    def h(req):
        if req.url.path.endswith("/oauth/accesstoken"):
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 1799})
        calls.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer OLD":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=ok_body())
    r = make_client(h).case_status("ZAR1234567890")
    assert r.ok and calls == ["Bearer OLD", "Bearer NEW"]


def test_not_found_and_429_and_5xx():
    def h404(req):
        if "oauth" in req.url.path:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 1799})
        return httpx.Response(404, json={"message": "not found"})
    assert make_client(h404).case_status("ZAR1234567890").status == "NOT_FOUND"

    def h429(req):
        if "oauth" in req.url.path:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 1799})
        return httpx.Response(429)
    with pytest.raises(RateLimited):
        make_client(h429).case_status("ZAR1234567890")

    def h500(req):
        if "oauth" in req.url.path:
            return httpx.Response(200, json={"access_token": "T", "expires_in": 1799})
        return httpx.Response(503)
    r = make_client(h500).case_status("ZAR1234567890")
    assert not r.ok and r.error == "HTTP 503"


class FakeClient:
    def __init__(self, results):
        self.results = list(results)

    def case_status(self, receipt):
        assert receipt == "ZAR1234567890"
        return self.results.pop(0)


@pytest.fixture()
def cn():
    cn = db.connect(); db.init(cn)
    for t in ("jobs", "checks", "cases", "users"):
        cn.execute(f"DELETE FROM {t}")
    cn.execute("INSERT INTO users(email,pw_hash,created_at,paid_until) VALUES('u@example.com','x',?,?)",
               (db.iso(db.now()), db.in_(days=30)))
    uid = cn.execute("SELECT id FROM users").fetchone()["id"]
    cn.execute("INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at) "
               "VALUES(?,?,?,?,?,?,?)", (uid, "Fam", security.encrypt("A123456789"),
                                         security.encrypt("ZAR1234567890"), "789", "7890", db.iso(db.now())))
    return cn


def test_worker_stores_status_and_emails_on_change(cn, monkeypatch):
    from tracker.uscis_api import CaseStatus
    sent = []
    monkeypatch.setattr(uscis_run.mailer, "send", lambda to, subj, body: sent.append((to, subj)))
    fc = FakeClient([CaseStatus(True, "Case Was Received", "d1", "I-589", "", "2026-10-01", 200),
                     CaseStatus(True, "Interview Was Scheduled", "d2", "I-589", "", "2026-11-01", 200)])
    [case] = uscis_run.due_cases(cn)
    assert uscis_run.process(cn, case, fc) == "Case Was Received"
    assert sent == []                         # first status: no email
    assert uscis_run.due_cases(cn) == []      # not due again until tomorrow
    cn.execute("UPDATE cases SET uscis_next_at=NULL")
    [case] = uscis_run.due_cases(cn)
    uscis_run.process(cn, case, fc)
    c = cn.execute("SELECT * FROM cases").fetchone()
    assert c["uscis_status"] == "Interview Was Scheduled" and c["uscis_modified"] == "2026-11-01"
    assert sent and "Interview Was Scheduled" in sent[0][1]


def test_worker_error_retries_in_an_hour(cn):
    from tracker.uscis_api import CaseStatus
    [case] = uscis_run.due_cases(cn)
    assert uscis_run.process(cn, case, FakeClient([CaseStatus(False, error="HTTP 503")])) == "ERROR"
    c = cn.execute("SELECT * FROM cases").fetchone()
    assert c["uscis_status"] is None and c["uscis_error"] == "HTTP 503" and c["uscis_next_at"]


def test_init_migrates_old_db(tmp_path):
    p = str(tmp_path / "old.sqlite3")
    old = db.connect(p)
    old.executescript(db.SCHEMA)
    old.execute("ALTER TABLE cases DROP COLUMN note")   # any old shape without uscis_* columns
    db.init(old)
    cols = {r["name"] for r in old.execute("PRAGMA table_info(cases)")}
    assert {"uscis_status", "uscis_next_at"} <= cols
