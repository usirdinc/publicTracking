import re
import pytest
from contextlib import closing
from urllib.parse import urlsplit,parse_qs
from fastapi.testclient import TestClient
from app import db,security,password_reset,config
from app.main import app
from test_app import client,csrf,signup,make_paid,CLIENT_DETAILS


@pytest.fixture(autouse=True)
def clear_reset_limits():
    with closing(db.connect()) as cn:
        db.init(cn)
        cn.execute('DELETE FROM password_reset_requests')

def request_link(client,monkeypatch,email='a@example.com'):
    from app import mailer
    sent=[]
    monkeypatch.setattr(mailer,'send',lambda *args:sent.append(args) or True)
    response=client.post('/forgot-password',data={'email':email,'csrf':csrf(client,'/forgot-password')})
    return response,sent


def test_reset_requests_are_generic_and_tokens_not_stored_plaintext(client,monkeypatch):
    signup(client)
    known,sent=request_link(client,monkeypatch)
    unknown,other=request_link(client,monkeypatch,'missing@example.com')
    assert password_reset.MESSAGE in known.text and password_reset.MESSAGE in unknown.text
    assert len(sent)==1 and not other
    url=re.search(r'https?://\S+',sent[0][2])[0]
    assert urlsplit(url).query=='' and urlsplit(url).fragment.startswith('token=')
    token=parse_qs(urlsplit(url).fragment)['token'][0]
    with closing(db.connect()) as cn:
        row=cn.execute('SELECT * FROM password_resets').fetchone()
        assert token!=row['token_hash'] and len(row['token_hash'])==64
    assert client.post('/forgot-password',data={'email':'a@example.com'}).status_code==403


def test_reset_is_single_use_revokes_old_sessions_and_preserves_client_data(client,monkeypatch):
    signup(client);make_paid('a@example.com')
    client.post('/cases',data={**CLIENT_DETAILS,'a_numbers':'A123456789','receipt':'ZAR1234567890','csrf':csrf(client,'/app')})
    old_cookie=client.cookies.get('session')
    response,sent=request_link(client,monkeypatch)
    token=parse_qs(urlsplit(re.search(r'https?://\S+',sent[0][2])[0]).fragment)['token'][0]
    with closing(db.connect()) as cn:before=dict(cn.execute('SELECT * FROM cases').fetchone())
    reset=TestClient(app)
    data={'token':token,'password':'a new secure password','confirm_password':'a new secure password','csrf':csrf(reset,'/reset-password')}
    result=reset.post('/reset-password',data=data)
    assert 'Password updated' in result.text
    with closing(db.connect()) as cn:
        user=cn.execute('SELECT * FROM users').fetchone()
        assert security.verify_password(user['pw_hash'],'a new secure password')
        assert not security.verify_password(user['pw_hash'],'correct horse battery')
        assert dict(cn.execute('SELECT * FROM cases').fetchone())==before
        assert not cn.execute('SELECT * FROM password_resets').fetchall()
    stale=TestClient(app);stale.cookies.set('session',old_cookie)
    assert stale.get('/account/settings',follow_redirects=False).status_code==303
    data['csrf']=csrf(reset,'/reset-password')
    assert 'invalid or expired' in reset.post('/reset-password',data=data).text
    login=reset.post('/login',data={'email':'a@example.com','password':'a new secure password','csrf':csrf(reset,'/login')},follow_redirects=False)
    assert login.headers['location']=='/app'
    assert reset.get('/account/settings').status_code==200


def test_expired_tokens_and_password_mismatch_cannot_change_password(client,monkeypatch):
    signup(client)
    response,sent=request_link(client,monkeypatch)
    token=parse_qs(urlsplit(re.search(r'https?://\S+',sent[0][2])[0]).fragment)['token'][0]
    with closing(db.connect()) as cn:
        original=cn.execute('SELECT pw_hash FROM users').fetchone()[0]
        cn.execute("UPDATE password_resets SET expires_at='2000-01-01T00:00:00+00:00'")
    data={'token':token,'password':'a new secure password','confirm_password':'a new secure password','csrf':csrf(client,'/reset-password')}
    assert 'invalid or expired' in client.post('/reset-password',data=data).text
    data['confirm_password']='a different secure password';data['csrf']=csrf(client,'/reset-password')
    assert 'matching passwords' in client.post('/reset-password',data=data).text
    with closing(db.connect()) as cn:assert cn.execute('SELECT pw_hash FROM users').fetchone()[0]==original
    assert client.post('/reset-password',data={'token':token,'password':'a new secure password','confirm_password':'a new secure password'}).status_code==403


def test_reset_email_rate_limits_are_durable(client,monkeypatch):
    signup(client)
    from app import mailer
    sent=[];monkeypatch.setattr(mailer,'send',lambda *args:sent.append(args) or True)
    for _ in range(4):
        page=client.post('/forgot-password',data={'email':'a@example.com','csrf':csrf(client,'/forgot-password')})
        assert password_reset.MESSAGE in page.text
    assert len(sent)==2


def test_reset_pages_are_private_and_localized(client):
    assert '/forgot-password' in client.get('/login').text
    for path in ['/forgot-password','/reset-password']:
        response=client.get(path)
        assert 'no-store' in response.headers['cache-control']
        assert response.headers['referrer-policy']=='no-referrer'
    client.post('/language',data={'language':'my','next':'/login','csrf':csrf(client,'/login')})
    assert 'စကားဝှက် ပြန်သတ်မှတ်ရန်' in client.get('/forgot-password').text
    assert 'စကားဝှက်အသစ်' in client.get('/reset-password').text
