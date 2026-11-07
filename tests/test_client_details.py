import json

from fastapi.testclient import TestClient
from app import db, security
from app.main import app
from test_app import CLIENT_DETAILS, client, csrf, make_paid, signup


def test_case_intake_requires_details_and_encrypts_them(client):
    signup(client)
    assert client.get("/cases/new", follow_redirects=False).headers["location"] == "/subscribe"
    make_paid("a@example.com")
    token = csrf(client, "/cases/new")
    invalid = client.post("/cases", data={**CLIENT_DETAILS, "a_numbers": "A123456789",
                                          "receipt": "invalid", "csrf": token})
    assert invalid.status_code == 422
    assert "no-store" in invalid.headers["cache-control"]
    assert 'name="email"' not in invalid.text
    assert 'value="A123456789"' in invalid.text
    payload = {"a_numbers": "A123456789", "receipt": "ZAR1234567890", "csrf": token}
    for invalid_contact in ({"email": "wrong..address@example.com"}, {"phone": "2025550142 ext 3"}):
        response = client.post("/cases", data={**payload, **CLIENT_DETAILS, **invalid_contact})
        assert response.status_code == 422
    client.post("/cases", data=payload)
    cn = db.connect()
    assert cn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    client.post("/cases", data={**payload, **CLIENT_DETAILS})
    case = cn.execute("SELECT id, client_details_enc FROM cases").fetchone()
    assert case and "contact@example.com" not in case["client_details_enc"]
    assert json.loads(security.decrypt(case["client_details_enc"])) == {**CLIENT_DETAILS, "phone": "+12025550142"}
    assert "contact@example.com" not in client.get("/app").text
    path = f"/cases/{case['id']}/details"
    response = client.get(path)
    assert 'name="email"' not in response.text
    assert 'value="Test"' in response.text
    assert "no-store" in response.headers["cache-control"]
    assert client.post(path, data={**CLIENT_DETAILS, "csrf": "bad"}).status_code == 403
    other = TestClient(app)
    signup(other, "other@example.com")
    make_paid("other@example.com")
    assert other.get(path).status_code == 404
    assert other.post(path, data={**CLIENT_DETAILS, "csrf": csrf(other, "/app")}).status_code == 404
    assert cn.execute("SELECT client_details_enc FROM cases").fetchone()[0] == case["client_details_enc"]

def test_account_tracking_limits_count_unique_people_across_cases(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "NEW_CASES_PER_DAY", 10)
    signup(client)
    make_paid("a@example.com")
    token = csrf(client, "/cases/new")
    payload = {**CLIENT_DETAILS, "csrf": token}

    def add(numbers, serial):
        return client.post("/cases", data={**payload, "a_numbers": numbers,
                                           "receipt": f"ZAR{serial:010d}"})

    assert "Case added" in add("123456781,123456782,123456783,123456784", 1).text
    assert add("123456785", 2).status_code == 422
    for serial in (2, 3, 4):
        assert "Case added" in add("123456781", serial).text
    assert "up to 4 cases" in add("123456781", 5).text
