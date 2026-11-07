"""Square card-on-file subscriptions. Card details are handled by Square's SDK.

The server receives a single-use token, never a card number or CVV. Subscription
creation does not grant access: only the existing signed invoice webhook does.
"""
import logging
import uuid
from datetime import timedelta

import httpx

from app import config, db, security, square


class BillingError(Exception):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def enabled():
    return bool(config.SQUARE_NATIVE_CHECKOUT and config.SQUARE_APPLICATION_ID
                and config.SQUARE_LOCATION_ID and config.SQUARE_PLAN_VARIATION_ID
                and config.SQUARE_ACCESS_TOKEN and config.SQUARE_WEBHOOK_SIGNATURE_KEY
                and config.DATA_KEY)


def enrollment(cn, uid):
    return cn.execute('SELECT * FROM billing_enrollments WHERE user_id=?', (uid,)).fetchone()


class SquareBilling:
    def __init__(self, http=None):
        self.owns_http = http is None
        self.http = http or httpx.Client(timeout=httpx.Timeout(15))
        # Provider request bodies and single-use tokens must never reach logs.
        logging.getLogger('httpx').setLevel(logging.WARNING)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.owns_http:
            self.http.close()

    def api(self, method, path, body=None):
        try:
            response = self.http.request(method, square.api_base() + path, json=body,
                headers={'Authorization': 'Bearer ' + config.SQUARE_ACCESS_TOKEN,
                         'Square-Version': square.SQUARE_VERSION})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise BillingError('Square could not confirm the request. Please try again shortly.', uncertain=True) from None
        if not isinstance(data, dict) or response.status_code >= 500 or response.status_code in (408, 429):
            raise BillingError('Square is temporarily unavailable. Please try again shortly.', uncertain=True)
        if response.status_code >= 400 or data.get('errors'):
            raise BillingError('Square could not complete this request. Check your card details or try another card.')
        return data

    def validate_plan(self, plan_id):
        data = self.api('GET', '/v2/catalog/object/' + plan_id)
        obj = data.get('object', {})
        phases = obj.get('subscription_plan_variation_data', {}).get('phases', [])
        price = phases[0].get('pricing', {}) if len(phases) == 1 else {}
        money = price.get('price_money') or price.get('price') or {}
        if not (obj.get('type') == 'SUBSCRIPTION_PLAN_VARIATION' and len(phases) == 1
                and phases[0].get('cadence') == 'MONTHLY' and price.get('type') == 'STATIC'
                and money == {'amount': 1000, 'currency': 'USD'}):
            raise BillingError('This subscription plan is not available. Please contact USIRD.')

    def subscribe(self, cn, user, token, cardholder):
        uid = user['id']
        with db.tx(cn):
            user = cn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not user:
                raise BillingError('Please log in again.')
            if db.is_paid(user):
                return 'paid'
            old = enrollment(cn, uid)
            if not old and user['square_customer_id'] and user['subscription_status'] in ('ACTIVE', 'PENDING', 'PAUSED'):
                raise BillingError('You already have a Square subscription. Please use its invoice or contact USIRD for help.')
            if old and old['subscription_id'] and old['state'] != 'ended':
                return 'pending'
            if old and old['state'] == 'processing' and db.parse(old['updated_at']) > db.now() - timedelta(seconds=90):
                return 'pending'
            if old and old['state'] == 'processing' and not old['card_enc'] and not token:
                # No card was persisted, so no subscription request could have run.
                cn.execute("UPDATE billing_enrollments SET state='failed' WHERE user_id=?", (uid,))
                return 'retry'
            # An ambiguous subscription response must reuse its original card,
            # location, plan and idempotency key; never start another charge.
            resume = bool(old and old['state'] in ('uncertain', 'processing') and old['card_enc'])
            if not resume:
                if not token or len(token) > 16384 or not cardholder or len(cardholder) > 120:
                    raise BillingError('Enter your card details and the name on the card.')
                rid = str(uuid.uuid4())
                cn.execute('INSERT INTO billing_enrollments(user_id,request_id,authorized_at,state,location_id,plan_id,updated_at) '
                    'VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET request_id=excluded.request_id,authorized_at=excluded.authorized_at, '
                    'state=excluded.state,location_id=excluded.location_id,plan_id=excluded.plan_id, '
                    'customer_id=NULL,card_enc=NULL,subscription_id=NULL,cancel_scheduled=0,updated_at=excluded.updated_at',
                    (uid, rid, db.iso(db.now()), 'processing', config.SQUARE_LOCATION_ID, config.SQUARE_PLAN_VARIATION_ID, db.iso(db.now())))
            else:
                cn.execute("UPDATE billing_enrollments SET state='processing',updated_at=? WHERE user_id=?",
                           (db.iso(db.now()), uid))
            row = enrollment(cn, uid)
        stage = 'plan'
        try:
            self.validate_plan(row['plan_id'])
            stage = 'customer'
            cid = row['customer_id'] or user['square_customer_id']
            if not cid:
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, config.BASE_URL + '/customer/' + str(uid)
                                    + '/' + user['created_at'] + '/' + user['email']))
                cid = self.api('POST', '/v2/customers', {'idempotency_key': key,
                    'email_address': user['email'], 'reference_id': str(uid)})['customer']['id']
            # Match webhook notifications before subscription creation can send one.
            with db.tx(cn):
                cn.execute('UPDATE users SET square_customer_id=? WHERE id=?', (cid, uid))
                cn.execute('UPDATE billing_enrollments SET customer_id=?,updated_at=? WHERE user_id=?',
                           (cid, db.iso(db.now()), uid))
            stage = 'card'
            if row['card_enc']:
                card_id = security.decrypt(row['card_enc'])
            else:
                card_id = self.api('POST', '/v2/cards', {'idempotency_key': row['request_id'],
                    'source_id': token, 'card': {'customer_id': cid, 'cardholder_name': cardholder,
                                              'reference_id': row['request_id']}})['card']['id']
                cn.execute('UPDATE billing_enrollments SET card_enc=?,updated_at=? WHERE user_id=?',
                           (security.encrypt(card_id), db.iso(db.now()), uid))
            stage = 'subscription'
            body = {'idempotency_key': row['request_id'], 'location_id': row['location_id'],
                    'plan_variation_id': row['plan_id'], 'customer_id': cid, 'card_id': card_id}
            data = self.api('POST', '/v2/subscriptions', body)
            sid = data['subscription']['id']
            # A fast invoice webhook might already have marked this row paid.
            cn.execute("UPDATE billing_enrollments SET subscription_id=?,state=CASE WHEN state='paid' THEN state ELSE 'pending' END,updated_at=? WHERE user_id=?",
                       (sid, db.iso(db.now()), uid))
            return 'pending'
        except BillingError as error:
            state = 'uncertain' if resume or (stage == 'subscription' and error.uncertain) else 'failed'
            cn.execute('UPDATE billing_enrollments SET state=?,updated_at=? WHERE user_id=? AND state<>?',
                       (state, db.iso(db.now()), uid, 'paid'))
            if state == 'uncertain':
                return 'uncertain'
            raise
        except (KeyError, TypeError):
            # A malformed response after a charge request is ambiguous too.
            state = 'uncertain' if resume or stage == 'subscription' else 'failed'
            cn.execute("UPDATE billing_enrollments SET state=?,updated_at=? WHERE user_id=? AND state<>'paid'",
                       (state, db.iso(db.now()), uid))
            if state == 'uncertain':
                return 'uncertain'
            raise BillingError('Square could not confirm the request. Please try again shortly.') from None

    def invoice_url(self, cn, uid):
        row = enrollment(cn, uid)
        if not row or not row['subscription_id']:
            return None
        sub = self.api('GET', '/v2/subscriptions/' + row['subscription_id'])['subscription']
        for iid in reversed(sub.get('invoice_ids', [])):
            inv = self.api('GET', '/v2/invoices/' + iid)['invoice']
            if inv.get('status') in ('UNPAID', 'PARTIALLY_PAID', 'PAYMENT_PENDING'):
                return inv.get('public_url')
        return None

    def cancel(self, cn, uid):
        row = enrollment(cn, uid)
        if not row or not row['subscription_id'] or row['cancel_scheduled']:
            return
        data = self.api('POST', '/v2/subscriptions/' + row['subscription_id'] + '/cancel', {})
        if (data.get('subscription') or {}).get('id') != row['subscription_id']:
            raise BillingError('Square could not confirm cancellation. Please try again shortly.')
        cn.execute('UPDATE billing_enrollments SET cancel_scheduled=1 WHERE user_id=?', (uid,))
