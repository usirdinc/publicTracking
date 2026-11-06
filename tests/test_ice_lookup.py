from fastapi.testclient import TestClient

from app import db, security
from app.main import app
from test_app import CLIENT_DETAILS, client, csrf, signup, make_paid


def test_ice_lookup_uses_saved_numbers_and_protects_country_data(client):
    signup(client)
    make_paid("a@example.com")
    client.post("/cases", data={**CLIENT_DETAILS,
        "a_numbers": "A123456789;A987654321", "receipt": "ZAR1234567890",
        "csrf": csrf(client, "/app"),
    })
    cn = db.connect()
    case_id = cn.execute("SELECT id FROM cases").fetchone()[0]
    path = f"/cases/{case_id}/ice"
    page = client.get(path)
    assert "ending in 789" in page.text and "ending in 321" in page.text
    assert "123456789" not in page.text and "987654321" not in page.text
    assert "no-store" in page.headers["cache-control"]
    assert client.post(path, data={"person": 1, "country": "Burma", "action": "prepare"}).status_code == 403
    token = csrf(client, path)
    for person, country in [(0, "Canada"), (1, "Burma")]:
        result = client.post(path, data={
            "csrf": token, "person": person, "country": country, "action": "prepare",
        })
        assert result.status_code == 200
        assert ("123456789" if person == 0 else "987654321") in result.text
        assert ("987654321" if person == 0 else "123456789") not in result.text
        assert "no-store" in result.headers["cache-control"]
    stored = cn.execute("SELECT ice_birth_countries_enc FROM cases").fetchone()[0]
    assert "Burma" not in stored and "Canada" not in stored
    assert "Burma" in security.decrypt(stored) and "Canada" in security.decrypt(stored)
    client.post("/logout")
    other = TestClient(app)
    signup(other, "other@example.com")
    make_paid("other@example.com")
    assert other.get(path).status_code == 404
    assert other.post(path, data={
        "csrf": csrf(other, "/app"), "person": 1, "country": "Mexico", "action": "prepare",
    }).status_code == 404
    assert cn.execute("SELECT ice_birth_countries_enc FROM cases").fetchone()[0] == stored


def test_staff_ice_queue_requires_opt_in_and_rejects_stale_results(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ICE_STAFF_ENABLED", True)
    monkeypatch.setattr(config, "LOOKUP_STAFF_EMAILS", {"staff@example.com"})
    signup(client)
    make_paid("a@example.com")
    client.post("/cases", data={**CLIENT_DETAILS,
        "a_numbers": "A123456789", "receipt": "ZAR1234567890",
        "csrf": csrf(client, "/app"),
    })
    cn = db.connect()
    case_id = cn.execute("SELECT id FROM cases").fetchone()[0]
    path = f"/cases/{case_id}/ice"
    staff = TestClient(app)
    signup(staff, "staff@example.com")
    queue = "/staff/lookups"
    result_path = f"{queue}/ice/{case_id}/0"
    assert staff.get("/admin", follow_redirects=False).status_code == 303
    assert "No ICE lookups are due." in staff.get(queue).text
    opt_in = {"csrf": csrf(client, path), "person": 0, "country": "Burma", "action": "save", "staff": "1"}
    client.post(path, data=opt_in)
    listing = staff.get(queue)
    assert "123456789" in listing.text and "Burma" in listing.text
    assert "no-store" in listing.headers["cache-control"]
    version = cn.execute("SELECT version FROM ice_staff_checks").fetchone()[0]
    fields = {"csrf": csrf(staff, queue), "status": "listed", "facility": "Example Facility", "version": version}
    assert client.post(result_path, data=fields).status_code == 403
    opt_in["country"] = "Canada"
    client.post(path, data=opt_in)
    staff.post(result_path, data=fields)
    assert cn.execute("SELECT result_enc FROM ice_staff_checks").fetchone()[0] is None
    fields["version"] = cn.execute("SELECT version FROM ice_staff_checks").fetchone()[0]
    staff.post(result_path, data=fields)
    row = cn.execute("SELECT * FROM ice_staff_checks").fetchone()
    assert "Example Facility" not in row["result_enc"]
    assert "Example Facility" in client.get(path).text
    assert "No ICE lookups are due." in staff.get(queue).text
    assert db.parse(row["next_at"]) > db.now()
    fields["status"] = "no_match"
    staff.post(result_path, data=fields)
    assert cn.execute("SELECT result_enc FROM ice_staff_checks").fetchone()[0] == row["result_enc"]
    opt_in["staff"] = ""
    client.post(path, data=opt_in)
    assert cn.execute("SELECT COUNT(*) FROM ice_staff_checks").fetchone()[0] == 0
