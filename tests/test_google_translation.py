import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import httpx
import pytest
from app import config
from app import google_translation as g
from app.uscis_burmese import translate_source

@pytest.fixture
def translation(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USCIS_AUTO_TRANSLATE_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_TRANSLATE_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setattr(config, "USCIS_TRANSLATION_CACHE_PATH", str(tmp_path/"cache.sqlite3"))
    monkeypatch.setattr(config, "USCIS_TRANSLATION_DAILY_CHAR_LIMIT", 10000)
    return tmp_path/"cache.sqlite3"

def test_background_translation_masks_identifiers_caches_and_restores_per_client(translation):
    source = "A new message for A123456789 and ZAR1234567890."
    assert g.lookup(source) == ("pending", None)
    calls = []
    def provider(request):
        calls.append(request)
        assert request.url == httpx.URL(g.API_URL)
        assert request.headers["X-goog-api-key"] == "fixture-not-a-real-key"
        text = json.loads(request.content)["q"]
        assert "A123456789" not in text and "ZAR1234567890" not in text
        return httpx.Response(200, json={"data":{"translations":[{"translatedText":"မြန်မာ " + text}]}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        assert g.process_one(client) == "ready"
        assert g.process_one(client) == "idle"
    state, result = g.lookup("A new message for A987654321 and ZAR9876543210.")
    assert state == "ready" and "A987654321" in result and "ZAR9876543210" in result
    assert "A123456789" not in result and len(calls) == 1
    disk = translation.read_bytes()
    assert b"A new message" not in disk and "မြန်မာ".encode() not in disk
    assert b"123456789" not in disk
    rendered = translate_source(source, automatic=True)
    assert rendered.machine_parts == 1 and rendered.complete
    assert source in str(rendered.original)

def test_disabled_never_queues_or_calls_google(translation,monkeypatch):
    monkeypatch.setattr(config,"USCIS_AUTO_TRANSLATE_ENABLED",False)
    assert g.lookup("Unknown wording") == ("unavailable",None)
    assert g.process_one() == "disabled"
    assert not translation.exists()

def test_daily_cap_is_atomic_across_workers(translation,monkeypatch):
    monkeypatch.setattr(config,"USCIS_TRANSLATION_DAILY_CHAR_LIMIT",20)
    assert g.lookup("Unknown sentence one") == ("pending",None)
    assert g.lookup("Unknown sentence two") == ("pending",None)
    calls=[]
    def provider(request):
        calls.append(request)
        return httpx.Response(200,json={"data":{"translations":[{"translatedText":"ဘာသာပြန်"}]}})
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:g.process_one(client),range(2)))
    assert sorted(results)==["budget","ready"] and len(calls)==1
    with closing(g.connect()) as cn:
        assert cn.execute("SELECT characters FROM usage").fetchone()[0] == 20
    assert g.lookup("Unknown sentence two")[0] in ("ready","unavailable")

def test_provider_cannot_drop_identifiers_or_introduce_html(translation):
    assert g.lookup("Review receipt ZAR1234567890") == ("pending",None)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"data":{"translations":[{"translatedText":"ဘာသာပြန်"}]}}))) as client:
        assert g.process_one(client)=="queued"
    assert g.lookup("Review receipt ZAR1234567890")[0] == "pending"
    protected=g.protect("Contact person@example.com A 123 456 789 ZAR1234567890")
    assert "person@example.com" not in protected.text and "1234567890" not in protected.text
    with pytest.raises(ValueError):protected.restore("ဘာသာပြန်")

def test_display_pending_and_safe_machine_html(translation):
    first=translate_source("Unknown agency wording", automatic=True)
    assert first.pending_parts == 1 and not first.complete
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"data":{"translations":[{"translatedText":"ဘာသာပြန် <img src=x onerror=alert(1)>"}]}}))) as client:
        assert g.process_one(client)=="ready"
    final=translate_source("Unknown agency wording", automatic=True)
    assert final.complete and "<img" not in str(final.burmese)
    assert "&lt;img" in str(final.burmese)

def test_google_failure_keeps_original_and_retry_is_bounded(translation):
    g.lookup("Unknown response")
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(403,json={"secret":"must never be logged"}))) as client:
        for expected in ["queued","queued","failed"]:
            assert g.process_one(client)==expected
            with closing(g.connect()) as cn:cn.execute("UPDATE translations SET retry_at=0")
    assert g.lookup("Unknown response")==("unavailable",None)
    result=translate_source("Unknown response",automatic=True)
    assert "Unknown response" in str(result.original) and not result.complete

from test_app import CLIENT_DETAILS, client, csrf, make_paid, signup
from app import db

def test_only_paid_burmese_dashboard_queues_source_text(translation,client):
    signup(client)
    make_paid('a@example.com')
    client.post('/cases',data={**CLIENT_DETAILS,'a_numbers':'A123456789','receipt':'ZAR1234567890','csrf':csrf(client,'/app')})
    cn=db.connect()
    cn.execute("UPDATE cases SET uscis_status='A newly introduced status',uscis_desc='New official agency wording',uscis_checked_at='2026-11-07T12:00:00+00:00'")
    before=dict(cn.execute('SELECT * FROM cases').fetchone())
    assert client.get('/app').status_code==200
    assert not translation.exists()
    client.post('/language',data={'language':'my','csrf':csrf(client,'/app'),'next':'/app'})
    page=client.get('/app')
    assert 'data-translation-pending="1"' in page.text
    assert translation.exists()
    assert dict(cn.execute('SELECT * FROM cases').fetchone())==before
    cn.close()
    with closing(g.connect()) as cache:
        assert cache.execute('SELECT COUNT(*) FROM translations').fetchone()[0]==2
