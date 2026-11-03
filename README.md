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
- Instant checks: 1 per case per day; 3 new cases per day; 5 cases per account; beta cap 200.
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
