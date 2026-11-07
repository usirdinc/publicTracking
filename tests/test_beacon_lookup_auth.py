import hashlib
from fastapi.testclient import TestClient
from app import config
from app.main import app, LOOKUP_COOKIE
from test_app import client


def test_beacon_session_is_limited_to_lookup_routes(client, monkeypatch):
    monkeypatch.setattr(config, "BASE_URL", "https://testserver")
    monkeypatch.setattr(config, "LOOKUP_MACHINE_TOKEN_SHA256", hashlib.sha256(b"test-token").hexdigest())
    monkeypatch.setattr(config, "ICE_STAFF_ENABLED", True)
    monkeypatch.setattr(config, "EOIR_ENABLED", True)
    beacon = TestClient(app, base_url="https://testserver")
    path = "/staff/lookups/session"
    assert beacon.post(path).status_code == 403
    assert beacon.get("/staff/lookups", follow_redirects=False).headers["location"] == "/login"
    result = beacon.post(path, headers={"Authorization": "Bearer test-token"})
    assert result.status_code == 204 and LOOKUP_COOKIE in result.cookies
    assert "HttpOnly" in result.headers["set-cookie"] and "Secure" in result.headers["set-cookie"]
    assert beacon.get("/staff/lookups").status_code == 200
    assert "EOIR lookup queue" in beacon.get("/admin/eoir").text
    assert beacon.get("/admin", follow_redirects=False).headers["location"] == "/login"
    assert beacon.get("/app", follow_redirects=False).headers["location"] == "/login"
    assert beacon.post("/staff/lookups/ice/1/0").status_code == 403
    monkeypatch.setattr(config, "LOOKUP_MACHINE_TOKEN_SHA256", "rotated")
    assert beacon.get("/staff/lookups", follow_redirects=False).headers["location"] == "/login"
