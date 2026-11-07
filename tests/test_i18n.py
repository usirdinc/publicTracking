import re
from fastapi.testclient import TestClient
from app import db, i18n, security
from app.main import app, templates
from test_app import CLIENT_DETAILS, client, csrf, make_paid, signup


def choose(client, locale, path="/", **extra):
    token = csrf(client, path)
    return client.post("/language", data={"language": locale, "csrf": token, "next": path, **extra},
                       follow_redirects=False)


def test_language_persists_across_public_pages_and_is_private(client):
    first = client.get("/")
    assert '<html lang="en">' in first.text
    response = choose(client, "my")
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    for path, expected in [("/", "အသုံးပြုပုံ"), ("/signup", "သင့်အမှုများကို စတင်စောင့်ကြည့်ပါ"),
                           ("/login", "အကောင့်ဝင်ရန်")]:
        page = client.get(path)
        assert page.status_code == 200
        assert '<html lang="my">' in page.text and expected in page.text
        assert page.headers["content-language"] == "my"
        assert "no-store" in page.headers["cache-control"]
        assert "Cookie" in page.headers["vary"]
    choose(client, "en")
    assert "Know when your asylum fee" in client.get("/").text


def test_language_endpoint_rejects_csrf_and_unknown_language_and_external_redirect(client):
    assert client.post("/language", data={"language": "my", "csrf": "wrong"}).status_code == 400
    assert choose(client, "other").status_code == 400
    assert choose(client, "my", next="//example.com").headers["location"] == "/"
    assert choose(client, "en", next="/\\example.com").headers["location"] == "/"
    assert choose(client, "my", next="/app").headers["location"] == "/app"
    response = client.post("/language", data={"language": "en", "csrf": csrf(client)},
                           headers={"Accept": "application/json"})
    assert response.json() == {"ok": True, "language": "en"}


def test_burmese_dashboard_preserves_case_data_and_official_text(client):
    signup(client); make_paid("a@example.com")
    client.post("/cases", data={**CLIENT_DETAILS, "a_numbers": "A123456789",
                                "receipt": "ZAR1234567890", "label": "Active", "csrf": csrf(client, "/app")})
    cn = db.connect()
    cn.execute("UPDATE cases SET status='DUE',amount='100',uscis_status='Interview Was Scheduled',uscis_desc='Official USCIS source text.',uscis_checked_at='2026-11-07T12:00:00+00:00'")
    before = dict(cn.execute("SELECT * FROM cases").fetchone())
    choose(client, "my", "/app")
    page = client.get("/app")
    assert "ကြေးပေးဆောင်ရန် လိုအပ်သည်" in page.text
    assert '<h2 data-no-translate>Active</h2>' in page.text
    assert "Official USCIS source text." in page.text and "Interview Was Scheduled" in page.text
    assert "data-source-text" in page.text and "$100" in page.text
    assert dict(cn.execute("SELECT * FROM cases").fetchone()) == before
    assert "A123456789" not in page.text and "ZAR1234567890" not in page.text
    cn.close()


def test_burmese_validation_keeps_identifiers_and_policy(client):
    signup(client); make_paid("a@example.com")
    choose(client, "my", "/cases/new")
    response = client.post("/cases", data={**CLIENT_DETAILS,"a_numbers":"A123456789",
                                          "receipt":"invalid","csrf":csrf(client,"/cases/new")})
    assert response.status_code == 422
    assert "လက်ခံစာနံပါတ်သည် ABC1234567890" in response.text
    assert 'value="A123456789"' in response.text
    assert db.connect().execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert i18n.translate("Fee due: $100", "my") == "ပေးဆောင်ရမည့်ကြေး — $100"
    assert "4" in i18n.translate("You can track up to 4 people per account.", "my")


def test_translation_placeholders_match_and_templates_have_catalog_entries():
    from jinja2 import nodes
    placeholders = lambda text: set(re.findall(r"\{(\w+)\}", text))
    for source, target in i18n.CATALOG.items():
        assert placeholders(source) == placeholders(target), source
    customer_templates = ["base.html","index.html","login.html","signup.html","subscribe.html",
                          "dashboard.html","account_settings.html","verify_phone.html","case_new.html",
                          "case_details.html","_client_details_fields.html","_tracking_guides.html","_stepper.html"]
    for name in customer_templates:
        tree = templates.env.parse(templates.env.loader.get_source(templates.env,name)[0])
        for call in tree.find_all(nodes.Call):
            if isinstance(call.node,nodes.Name) and call.node.name=="_" and call.args and isinstance(call.args[0],nodes.Const):
                assert call.args[0].value in i18n.CATALOG, (name, call.args[0].value)
