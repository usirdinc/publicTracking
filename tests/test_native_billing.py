import json
import re

import httpx
import pytest

from app import billing, config, db, security
from app.main import app
from fastapi.testclient import TestClient
from test_app import csrf, signup, _sign


@pytest.fixture()
def checkout(monkeypatch):
    cn = db.connect(); db.init(cn)
    for table in ('jobs', 'checks', 'cases', 'users', 'webhook_events', 'settings'):
        cn.execute('DELETE FROM ' + table)
    import app.main as main
    main._attempts.clear()
    for key, value in dict(SQUARE_NATIVE_CHECKOUT=True, SQUARE_APPLICATION_ID='app-public',
                           SQUARE_LOCATION_ID='location-public', SQUARE_PLAN_VARIATION_ID='plan-public',
                           SQUARE_ACCESS_TOKEN='private-test-token').items():
        monkeypatch.setattr(config, key, value)
    calls = []
    behavior = {'timeout': False, 'price': 1000, 'decline': False}

    def provider(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        path = req.url.path
        if path.startswith('/v2/catalog/object/'):
            data = {'object': {'type': 'SUBSCRIPTION_PLAN_VARIATION', 'subscription_plan_variation_data': {
                'phases': [{'cadence': 'MONTHLY', 'pricing': {'type': 'STATIC',
                    'price_money': {'amount': behavior['price'], 'currency': 'USD'}}}]}}}
        elif path == '/v2/customers':
            data = {'customer': {'id': 'CUSTOMER-ONE'}}
        elif path == '/v2/cards':
            if behavior['decline']:
                return httpx.Response(400, json={'errors': [{'code': 'CARD_DECLINED'}]})
            data = {'card': {'id': 'CARD-PRIVATE'}}
        elif path == '/v2/subscriptions':
            if behavior['timeout']:
                raise httpx.ReadTimeout('ambiguous response', request=req)
            data = {'subscription': {'id': 'SUB-ONE', 'status': 'ACTIVE'}}
        elif path.endswith('/cancel'):
            data = {'subscription': {'id': 'SUB-ONE'}}
        elif path == '/v2/subscriptions/SUB-ONE':
            data = {'subscription': {'invoice_ids': ['INVOICE-ONE']}}
        elif path == '/v2/invoices/INVOICE-ONE':
            data = {'invoice': {'status': 'UNPAID', 'public_url': behavior.get('invoice_url', 'https://squareup.com/invoice/test')}}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    transport = httpx.Client(transport=httpx.MockTransport(provider))
    original = billing.SquareBilling
    monkeypatch.setattr(billing, 'SquareBilling', lambda: original(transport))
    client = TestClient(app)
    signup(client)
    return client, calls, behavior


def pay(client, **changes):
    fields = dict(csrf=csrf(client, '/subscribe'), consent='1', source_id='single-use-test-token', cardholder='Test Customer')
    fields.update(changes)
    return client.post('/billing/subscribe', data=fields)


def test_native_card_form_is_scoped_and_consent_explicit(checkout):
    c, _, _ = checkout
    response = c.get('/subscribe')
    assert 'data-square-checkout' in response.text and 'id="card-container"' in response.text
    consent = re.search(r'<input[^>]*name="consent"[^>]*>', response.text).group()
    assert 'required' in consent and 'checked' not in consent
    assert 'private-test-token' not in response.text
    assert 'web.squarecdn.com' in response.headers['content-security-policy']
    assert 'web.squarecdn.com' not in c.get('/').headers['content-security-policy']
    assert 'Beta' not in c.get('/').text and '(beta)' not in c.get('/terms').text


def test_native_requires_csrf_consent_and_session(checkout):
    c, calls, _ = checkout
    assert pay(c, csrf='wrong').status_code == 403
    assert pay(c, consent='').status_code == 400
    assert not calls
    c.post('/logout')
    assert pay(c, csrf='wrong').status_code == 401


def test_native_subscription_does_not_unlock_until_signed_paid_invoice(checkout):
    c, calls, _ = checkout
    assert pay(c).json()['state'] == 'pending'
    assert not c.get('/account/paid').json()['paid']
    assert c.get('/app', follow_redirects=False).headers['location'] == '/subscribe'
    cn = db.connect()
    row = cn.execute('SELECT * FROM billing_enrollments').fetchone()
    assert security.decrypt(row['card_enc']) == 'CARD-PRIVATE'
    assert 'single-use-test-token' not in str(dict(row)) and 'CARD-PRIVATE' not in str(dict(row))
    assert next(b for _, p, b in calls if p == '/v2/customers')['email_address'] == 'a@example.com'
    ev = {'event_id': 'native-invoice', 'type': 'invoice.payment_made', 'data': {'object': {'invoice': {
        'id': 'INVOICE-ONE', 'status': 'PAID', 'subscription_id': 'SUB-ONE',
        'primary_recipient': {'customer_id': 'CUSTOMER-ONE'}}}}}
    raw = json.dumps(ev).encode()
    assert c.post('/webhooks/square', content=raw, headers={'x-square-hmacsha256-signature': _sign(raw)}).status_code == 200
    assert c.get('/account/paid').json()['paid']
    assert c.get('/subscribe', follow_redirects=False).headers['location'] == '/app'
    before = cn.execute('SELECT paid_until FROM users').fetchone()[0]
    ev['event_id'] = 'same-invoice-new-event'
    raw = json.dumps(ev).encode()
    c.post('/webhooks/square', content=raw, headers={'x-square-hmacsha256-signature': _sign(raw)})
    assert cn.execute('SELECT paid_until FROM users').fetchone()[0] == before


def test_repeated_submission_does_not_create_another_subscription(checkout):
    c, calls, _ = checkout
    assert pay(c).json()['state'] == 'pending'
    assert pay(c).json()['state'] == 'pending'
    assert len([x for x in calls if x[1] == '/v2/subscriptions' and x[0] == 'POST']) == 1


def test_ambiguous_charge_resumes_exact_request_even_after_config_change(checkout, monkeypatch):
    c, calls, behavior = checkout
    behavior['timeout'] = True
    assert pay(c).json()['state'] == 'uncertain'
    first = next(b for _, p, b in calls if p == '/v2/subscriptions')
    monkeypatch.setattr(config, 'SQUARE_PLAN_VARIATION_ID', 'replacement-plan')
    monkeypatch.setattr(config, 'SQUARE_LOCATION_ID', 'replacement-location')
    behavior['timeout'] = False
    assert pay(c, source_id='', cardholder='').json()['state'] == 'pending'
    bodies = [b for _, p, b in calls if p == '/v2/subscriptions']
    assert bodies == [first, first]
    assert len([x for x in calls if x[1] == '/v2/cards']) == 1


def test_plan_price_mismatch_cannot_create_card_or_charge(checkout):
    c, calls, behavior = checkout
    behavior['price'] = 2000
    assert pay(c).status_code == 400
    assert all(method == 'GET' for method, _, _ in calls)


def test_declined_card_can_retry_without_subscription(checkout):
    c, calls, behavior = checkout
    behavior['decline'] = True
    assert pay(c).status_code == 400
    assert not [x for x in calls if x[1] == '/v2/subscriptions']
    behavior['decline'] = False
    assert pay(c).json()['state'] == 'pending'


def test_cancel_and_delete_keep_billing_owned(checkout):
    c, calls, _ = checkout
    pay(c)
    cn = db.connect()
    cn.execute('UPDATE users SET paid_until=?', (db.in_(days=30),))
    before = cn.execute('SELECT paid_until FROM users').fetchone()[0]
    assert c.post('/billing/cancel', data={'csrf': 'bad'}, follow_redirects=False).status_code == 303
    assert not [x for x in calls if x[1].endswith('/cancel')]
    c.post('/billing/cancel', data={'csrf': csrf(c, '/app')})
    assert cn.execute('SELECT paid_until FROM users').fetchone()[0] == before
    assert len([x for x in calls if x[1].endswith('/cancel')]) == 1
    c.post('/logout')
    signup(c, 'b@example.com')
    c.post('/billing/cancel', data={'csrf': csrf(c, '/subscribe')})
    assert len([x for x in calls if x[1].endswith('/cancel')]) == 1


def test_invoice_redirect_rejects_foreign_host(checkout):
    c, _, behavior = checkout
    pay(c)
    behavior['invoice_url'] = 'https://evil.example/invoice'
    assert c.get('/billing/invoice', follow_redirects=False).headers['location'] == '/subscribe'


def test_native_unconfirmed_subscription_cannot_be_deleted(checkout):
    c, _, behavior = checkout
    behavior['timeout'] = True
    pay(c)
    c.post('/account/delete', data={'csrf': csrf(c, '/subscribe')})
    assert db.connect().execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_invoice_before_subscription_response_retries_without_losing_event(checkout):
    c, _, behavior = checkout
    behavior['timeout'] = True
    pay(c)
    event = {'event_id': 'fast-invoice', 'type': 'invoice.payment_made', 'data': {'object': {'invoice': {
        'id': 'FAST-ONE', 'status': 'PAID', 'subscription_id': 'SUB-ONE',
        'primary_recipient': {'customer_id': 'CUSTOMER-ONE'}}}}}
    raw = json.dumps(event).encode()
    headers = {'x-square-hmacsha256-signature': _sign(raw)}
    assert c.post('/webhooks/square', content=raw, headers=headers).status_code == 503
    assert not db.connect().execute("SELECT 1 FROM webhook_events WHERE event_id='fast-invoice'").fetchone()
    behavior['timeout'] = False
    pay(c, source_id='', cardholder='')
    assert c.post('/webhooks/square', content=raw, headers=headers).status_code == 200
    assert c.get('/account/paid').json()['paid']


@pytest.mark.parametrize('subscription,status', [('OTHER-SUB','PAID'),('SUB-ONE','PARTIALLY_PAID')])
def test_unrelated_or_partial_invoice_does_not_unlock(checkout, subscription, status):
    c, _, _ = checkout
    pay(c)
    event = {'event_id': 'unpaid-invoice', 'type': 'invoice.payment_made', 'data': {'object': {'invoice': {
        'id': 'UNPAID-ONE', 'status': status, 'subscription_id': subscription,
        'primary_recipient': {'customer_id': 'CUSTOMER-ONE'}}}}}
    raw = json.dumps(event).encode()
    c.post('/webhooks/square', content=raw, headers={'x-square-hmacsha256-signature': _sign(raw)})
    assert not c.get('/account/paid').json()['paid']


def test_stale_pre_card_attempt_can_recover_without_another_charge(checkout):
    c, calls, _ = checkout
    cn = db.connect(); user = cn.execute('SELECT * FROM users').fetchone()
    cn.execute("""INSERT INTO billing_enrollments(user_id,request_id,authorized_at,state,location_id,plan_id,updated_at)
                  VALUES(?,?,?,'processing',?,?,?)""",
               (user['id'],'abandoned',db.in_(minutes=-5),'location-public','plan-public',db.in_(minutes=-5)))
    assert pay(c, consent='', source_id='', cardholder='').json()['state'] == 'retry'
    assert not calls
    assert 'data-square-checkout' in c.get('/subscribe').text


def test_pending_attempt_blocks_simultaneous_submission(checkout):
    c, calls, _ = checkout
    cn = db.connect(); user = cn.execute('SELECT * FROM users').fetchone()
    cn.execute("""INSERT INTO billing_enrollments(user_id,request_id,authorized_at,state,location_id,plan_id,updated_at)
                  VALUES(?,?,?,'processing',?,?,?)""",
               (user['id'],'in-flight',db.iso(db.now()),'location-public','plan-public',db.iso(db.now())))
    assert pay(c).json()['state'] == 'pending'
    assert not calls


def test_delete_native_account_cancels_before_removing_data(checkout):
    c, calls, _ = checkout
    pay(c)
    c.post('/account/delete', data={'csrf': csrf(c, '/subscribe')})
    assert len([x for x in calls if x[1].endswith('/cancel')]) == 1
    assert db.connect().execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0


def test_card_testing_attempts_are_rate_limited(checkout):
    c, _, behavior = checkout
    behavior['decline'] = True
    for _ in range(8):
        assert pay(c).status_code == 400
    assert pay(c).status_code == 429
