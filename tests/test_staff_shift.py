import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import pytest
from fastapi.testclient import TestClient
from app import config,db,security,staff_queue as q
from app.main import app
from tracker.staff_results import parse_result
from test_app import client,signup,make_paid,csrf,CLIENT_DETAILS

@pytest.fixture
def seeded(client,monkeypatch):
    monkeypatch.setattr(config,'EOIR_ENABLED',True)
    monkeypatch.setattr(config,'ICE_STAFF_ENABLED',True)
    signup(client,'admin@example.com')
    make_paid('admin@example.com')
    client.post('/cases',data={**CLIENT_DETAILS,'a_numbers':'A123456789','receipt':'ZAR1234567890','nationality':'Burma','csrf':csrf(client,'/app')})
    with closing(db.connect()) as cn:
        cid=cn.execute('SELECT id FROM cases').fetchone()[0]
        cn.execute('UPDATE cases SET ice_birth_countries_enc=? WHERE id=?',(security.encrypt('{"A123456789":"Burma"}'),cid))
        cn.execute('INSERT INTO ice_staff_checks(case_id,person) VALUES(?,0)',(cid,))
    return cid


def test_parallel_workers_get_distinct_assignments(seeded):
    with closing(db.connect()) as cn:
        source=dict(cn.execute('SELECT * FROM cases').fetchone())
        for index in range(1,5):
            cn.execute('INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at,nationality) VALUES(?,?,?,?,?,?,?,?)',
                (source['user_id'],'Example',source['a_numbers_enc'],source['receipt_enc'],'789','7890',db.iso(db.now()),'Burma'))
    def take(index):
        with closing(db.connect()) as cn:return q.claim(cn,'worker-'+str(index))
    with ThreadPoolExecutor(max_workers=4) as pool:jobs=list(pool.map(take,range(4)))
    assert len({(j['service'],j['case_id'],j['person']) for j in jobs})==4
    with closing(db.connect()) as cn:assert q.claim(cn,'fifth') is None


def test_expired_assignment_is_reclaimed_and_cannot_save(seeded):
    with closing(db.connect()) as cn:
        original=q.claim(cn,'one','eoir')
        cn.execute("UPDATE staff_lookup_leases SET expires_at='2000-01-01T00:00:00+00:00'")
        replacement=q.claim(cn,'two','eoir')
        assert replacement['case_id']==original['case_id'] and replacement['lease_token']!=original['lease_token']
        with pytest.raises(ValueError):q.complete(cn,original['lease_token'],'retry')
        assert not q.heartbeat(cn,original['lease_token'])
        assert q.heartbeat(cn,replacement['lease_token'])


def test_country_change_and_revoked_optin_reject_stale_saves(seeded):
    with closing(db.connect()) as cn:
        job=q.claim(cn,'one','ice')
        cn.execute('DELETE FROM ice_staff_checks WHERE case_id=?',(seeded,))
        assert q.complete(cn,job['lease_token'],'retry')=='stale'
        assert not cn.execute('SELECT * FROM staff_lookup_leases').fetchall()
        job=q.claim(cn,'one','eoir')
        cn.execute("UPDATE cases SET nationality='Canada'")
        assert q.complete(cn,job['lease_token'],'retry')=='stale'
        assert cn.execute('SELECT eoir_status FROM cases').fetchone()[0] is None


def test_retry_preserves_last_success_and_never_records_no_match(seeded):
    with closing(db.connect()) as cn:
        encrypted=security.encrypt('{"status":"listed","facility":"Previous Facility"}')
        cn.execute("UPDATE ice_staff_checks SET result_enc=?,checked_at='2026-01-01'",(encrypted,))
        job=q.claim(cn,'one','ice')
        assert q.complete(cn,job['lease_token'],'retry')=='deferred'
        row=cn.execute('SELECT * FROM ice_staff_checks').fetchone()
        assert row['result_enc']==encrypted and row['checked_at']=='2026-01-01'
        assert db.parse(row['next_at'])>db.now()
        assert cn.execute('SELECT outcome FROM staff_lookup_attempts').fetchone()[0]=='deferred'


def test_official_eoir_result_saved_once_with_daily_due(seeded):
    text='A-Number: 123-456-789\nYour next MASTER hearing is IN PERSON on March 3, 2027 at 8:30 AM\nJUDGE\nSmith, Jane\nThis case is pending'
    with closing(db.connect()) as cn:
        job=q.claim(cn,'one','eoir')
        assert q.complete(cn,job['lease_token'],'save',text,'https://acis.eoir.justice.gov/en/caseInformation')=='saved'
        row=cn.execute('SELECT * FROM cases').fetchone()
        assert row['eoir_status']=='CASE_FOUND' and '2027' in row['eoir_fields']
        assert 23*3600 < (db.parse(row['eoir_next_at'])-db.now()).total_seconds()<=24*3600
        with pytest.raises(ValueError):q.complete(cn,job['lease_token'],'save',text,'https://acis.eoir.justice.gov/en/caseInformation')


def test_unrecognized_and_wrong_identity_pages_are_never_saved(seeded):
    text='A-Number: 987-654-321\nThis case is pending'
    assert parse_result('eoir',text,'https://acis.eoir.justice.gov/en/caseInformation','123456789') is None
    assert parse_result('ice','No detainees found.','https://locator.ice.gov/odls/#/search','123456789') is None
    assert parse_result('ice','No detainees found.','https://evil.test/#/searchResults','123456789') is None
    assert parse_result('ice','Captcha error - unable to validate\nNo detainees found.','https://locator.ice.gov/odls/#/searchResults','123456789') is None
    with closing(db.connect()) as cn:
        job=q.claim(cn,'one','eoir')
        with pytest.raises(ValueError):q.complete(cn,job['lease_token'],'save','Please complete the security check','https://acis.eoir.justice.gov/en/')
        assert cn.execute('SELECT eoir_status FROM cases').fetchone()[0] is None


def test_ice_result_parser_requires_result_page_and_specific_evidence():
    number='123456789'
    assert parse_result('ice','No detainees found.','https://locator.ice.gov/odls/#/searchResults',number)=={'status':'no_match','facility':''}
    result=parse_result('ice','A-Number: 123-456-789\nCurrent Detention Facility\nExample Facility','https://locator.ice.gov/odls/#/detaineeDetails',number)
    assert result=={'status':'listed','facility':'Example Facility'}
    assert parse_result('ice','An unexpected changed page','https://locator.ice.gov/odls/#/searchResults',number) is None


def test_machine_auth_csrf_and_job_api(seeded,client,monkeypatch):
    # Staff API requires both an authorized session and CSRF; a client session alone is not enough.
    anonymous=TestClient(app)
    assert anonymous.get('/staff/lookups/shift').status_code==403
    assert client.post('/staff/lookups/shift/claim',data={'worker':'one'}).status_code==403
    response=client.get('/staff/lookups/shift')
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    token=response.json()['csrf']
    response=client.post('/staff/lookups/shift/claim',data={'csrf':token,'worker':'one','service':'eoir'})
    assert response.status_code==200
    job=response.json()['job']
    assert job['number']=='123456789'
    assert client.post('/staff/lookups/shift/result',data={'csrf':token,'lease_token':job['lease_token'],'action':'heartbeat'}).json()['outcome']=='extended'
    assert client.post('/staff/lookups/shift/result',data={'csrf':token,'lease_token':job['lease_token'],'action':'retry'}).json()['outcome']=='deferred'


def test_unpaid_accounts_cannot_starve_eligible_queue(seeded):
    with closing(db.connect()) as cn:
        source=dict(cn.execute('SELECT * FROM cases').fetchone())
        cn.execute("INSERT INTO users(email,pw_hash,created_at) VALUES('unpaid@example.test','not-used',?)",(db.iso(db.now()),))
        uid=cn.execute("SELECT id FROM users WHERE email='unpaid@example.test'").fetchone()[0]
        for index in range(205):
            cn.execute('INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at,nationality) VALUES(?,?,?,?,?,?,?,?)',
                (uid,'Unpaid example',source['a_numbers_enc'],source['receipt_enc'],'789','7890',db.iso(db.now()),'Burma'))
        assert q.claim(cn,'one','eoir')['case_id']==seeded


def test_machine_session_can_use_shift_but_not_general_admin(seeded,monkeypatch):
    monkeypatch.setattr(config,'BASE_URL','https://testserver')
    monkeypatch.setattr(config,'LOOKUP_MACHINE_TOKEN_SHA256',hashlib.sha256(b'fixture-workstation-token').hexdigest())
    machine=TestClient(app,base_url='https://testserver')
    assert machine.post('/staff/lookups/session',headers={'Authorization':'Bearer incorrect'}).status_code==403
    assert machine.post('/staff/lookups/session',headers={'Authorization':'Bearer fixture-workstation-token'}).status_code==204
    response=machine.get('/staff/lookups/shift')
    assert response.status_code==200
    token=response.json()['csrf']
    assert machine.post('/staff/lookups/shift/claim',data={'csrf':token,'worker':'one','service':'eoir'}).json()['job']
    assert machine.get('/admin',follow_redirects=False).status_code==303
    assert machine.get('/account/settings',follow_redirects=False).status_code==303


def test_unpaid_or_unverified_owners_and_disabled_services_do_not_queue(seeded,monkeypatch):
    with closing(db.connect()) as cn:
        cn.execute('UPDATE users SET phone_verification_required=1')
        assert q.claim(cn,'one') is None
        cn.execute('UPDATE users SET phone_verification_required=0')
        monkeypatch.setattr(config,'EOIR_ENABLED',False)
        monkeypatch.setattr(config,'ICE_STAFF_ENABLED',False)
        assert q.claim(cn,'one') is None


def test_four_workers_drain_1000_synthetic_jobs_without_duplicates(seeded,monkeypatch):
    monkeypatch.setattr(config,'ICE_STAFF_ENABLED',False)
    with closing(db.connect()) as cn:
        source=dict(cn.execute('SELECT * FROM cases').fetchone())
        with db.tx(cn):
            cn.executemany('INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at,nationality) VALUES(?,?,?,?,?,?,?,?)',
                [(source['user_id'],'Synthetic scale test',source['a_numbers_enc'],source['receipt_enc'],'789','7890',db.iso(db.now()),'Burma') for _ in range(999)])
    def drain(index):
        completed=[]
        with closing(db.connect()) as cn:
            while True:
                job=q.claim(cn,'worker-'+str(index),'eoir')
                if job is None:break
                assert q.complete(cn,job['lease_token'],'save','No case found for this A-Number.','https://acis.eoir.justice.gov/en/')=='saved'
                completed.append(job['case_id'])
        return completed
    with ThreadPoolExecutor(max_workers=4) as pool:
        completed=[cid for batch in pool.map(drain,range(4)) for cid in batch]
    assert len(completed)==1000 and len(set(completed))==1000
    with closing(db.connect()) as cn:
        assert cn.execute("SELECT COUNT(*) FROM cases WHERE eoir_status='NO_CASE'").fetchone()[0]==1000
        assert cn.execute('SELECT COUNT(*) FROM staff_lookup_leases').fetchone()[0]==0
