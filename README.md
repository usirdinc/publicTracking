# publicTracking

AAF Tracker: subscription web app that checks the public USCIS Annual Asylum Fee
page for a subscriber's A-Number + I-589 receipt, re-checks on a schedule and
emails on change. Runs on one VPS: FastAPI web app + one-browser worker + SQLite.

```
app/       web app (accounts, cases, Square webhook, admin)
worker/    run.py: AAF browser worker (job queue, pacing, block/error circuit breakers)
           uscis_run.py: USCIS case status via the official API (no browser), daily + on Check now
tracker/   AAF form fill + result parsing (ported from usird v2 aaf_check.py)
scripts/   aaf_batch_test.py (CSV test run against known answers)
deploy/    systemd units + install.sh
```

## Safety rules built in
- One browser, 3-8 s between checks. Block page: all checks pause 6 h + admin email.
  6 errors in a row: pause 1 h.
- A-Numbers and receipts encrypted at rest (Fernet, `DATA_KEY`); only last digits shown.
- Instant checks: 1 per case per day; 3 new cases per day; 5 cases per account; account capacity 200.
- No USCIS passwords, no operator Google profile.

## Deploy (VPS, user `tracker`)
    git clone https://github.com/usirdinc/publicTracking ~/publicTracking
    cd ~/publicTracking && cp .env.example .env && nano .env
    ./deploy/install.sh

Then point a Cloudflare tunnel public hostname (e.g. track.usird.org) at
`http://localhost:8000`, and set Square's webhook to `BASE_URL/webhooks/square`
with events `invoice.payment_made`, `subscription.created`, `subscription.updated`.

## Tests
    python -m pytest -q

### Embedded subscription checkout

Set `SQUARE_NATIVE_CHECKOUT=1` with the matching Square application, location and fixed-price plan variation IDs. The plan must have exactly one static $10 USD MONTHLY phase. The server token and signed `invoice.payment_made` webhook must already be configured. Leave the flag off to retain the hosted checkout link.

Square Web Payments SDK collects card details in its iframe on `/subscribe`. Only a single-use token reaches this server. Card-on-file IDs are encrypted with the existing DATA_KEY. Creating a subscription never grants access; a signed, fully paid invoice for that subscription does. Persistent attempt IDs prevent duplicate subscriptions, and ambiguous subscription responses must resume the same request. Account options allow cancellation; deleting an account first requests cancellation. Do not delete a pending/uncertain enrollment manually.

Only the checkout page permits Square's documented script, frame, connection and font origins and inline SDK styles. Other pages keep the original strict CSP. No card numbers, tokens or provider bodies should be logged. Run `python -m pytest -q` before release; use Sandbox for an actual test charge. Production form-mount verification does not authorize a real payment.
