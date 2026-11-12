import json
from contextlib import closing
from app import db,security,staff_queue as q,staff_notifications as delivery,notifications
from test_app import client
from test_staff_shift import seeded


def make_change(case_id):
    with closing(db.connect()) as cn:
        job=q.claim(cn,'one','eoir')
        assert q.complete(cn,job['lease_token'],'save','No case found for this A-Number.','https://acis.eoir.justice.gov/en/')=='saved'
        assert cn.execute('SELECT COUNT(*) FROM staff_notification_outbox').fetchone()[0]==0
        cn.execute('UPDATE cases SET eoir_next_at=NULL WHERE id=?',(case_id,))
        job=q.claim(cn,'one','eoir')
        text='A-Number: 123-456-789\nThis case is pending'
        q.complete(cn,job['lease_token'],'save',text,'https://acis.eoir.justice.gov/en/caseInformation')


def test_verified_changes_send_email_and_consented_sms_once(seeded,monkeypatch):
    emails=[];texts=[]
    monkeypatch.setattr(notifications.mailer,'send',lambda *a:emails.append(a) or True)
    monkeypatch.setattr(notifications.sms,'configured',lambda:True)
    monkeypatch.setattr(notifications.sms,'send',lambda *a:texts.append(a) or True)
    monkeypatch.setattr(notifications.phone_verification,'is_verified',lambda *a:True)
    with closing(db.connect()) as cn:
        uid=cn.execute('SELECT user_id FROM cases').fetchone()[0]
        profile={'email':'contact@example.com','email_enabled':True,'phone':'+12025550142','sms_enabled':True,
            'sms_consented_at':db.iso(db.now()),'sms_consent_phone':'+12025550142'}
        cn.execute('INSERT OR REPLACE INTO account_profiles(user_id,contact_enc) VALUES(?,?)',(uid,security.encrypt(json.dumps(profile))))
    make_change(seeded)
    assert delivery.drain_one() and not delivery.drain_one()
    assert len(emails)==1 and len(texts)==1
    assert '123456789' not in texts[0][1] and 'EOIR' in texts[0][1]
    with closing(db.connect()) as cn:
        cn.execute("UPDATE staff_notification_outbox SET state='pending',next_at='2000-01-01'")
    assert delivery.drain_one()
    assert len(emails)==1 and len(texts)==1


def test_failed_email_is_retried_without_resending_accepted_channels(seeded,monkeypatch):
    calls=[]
    monkeypatch.setattr(notifications.mailer,'send',lambda *a:calls.append(a) or len(calls)>1)
    make_change(seeded)
    assert delivery.drain_one()
    with closing(db.connect()) as cn:
        assert cn.execute('SELECT state FROM staff_notification_outbox').fetchone()[0]=='pending'
        cn.execute("UPDATE staff_notification_outbox SET next_at='2000-01-01'")
    assert delivery.drain_one() and len(calls)==2
    with closing(db.connect()) as cn:assert cn.execute('SELECT state FROM staff_notification_outbox').fetchone()[0]=='done'


def test_unchanged_or_deferred_results_do_not_notify(seeded):
    with closing(db.connect()) as cn:
        cn.execute("UPDATE cases SET eoir_status='NO_CASE',eoir_fields='{}'")
        job=q.claim(cn,'one','eoir')
        q.complete(cn,job['lease_token'],'save','No case found for this A-Number.','https://acis.eoir.justice.gov/en/')
        assert cn.execute('SELECT COUNT(*) FROM staff_notification_outbox').fetchone()[0]==0
        job=q.claim(cn,'one','ice');q.complete(cn,job['lease_token'],'retry')
        assert cn.execute('SELECT COUNT(*) FROM staff_notification_outbox').fetchone()[0]==0


def test_court_result_page_is_owner_only(seeded,client):
    from fastapi.testclient import TestClient
    from app.main import app
    from test_app import signup
    make_change(seeded)
    response=client.get(f'/cases/{seeded}/court')
    assert response.status_code==200 and 'Pending' in response.text
    assert '123456789' not in response.text and 'no-store' in response.headers['cache-control']
    other=TestClient(app);signup(other,'other@example.com')
    assert other.get(f'/cases/{seeded}/court').status_code==404
