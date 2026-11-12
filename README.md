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

### Daily staff lookup shift

Use a managed work computer and open `/staff/lookups` in a regular browser each day. Authorized staff perform the searches and complete any verification directly on the official websites. Record a result only after verifying it. Defer failures instead of recording them as no match.

- ICE: the owner opts in for each person on the ICE lookup page. Enter that person's A-Number and country of birth in the ICE locator. Save a listed result or no match. The item becomes due again after one day.
- EOIR: the owner supplies nationality for the first A-Number on a case. Open the EOIR queue, complete the official ACIS search, and paste the result in the queue. Use `EOIR_RECHECK_DAYS=1` for daily repeat checks.
- Enable the queues with `ICE_STAFF_ENABLED=1` and `EOIR_ENABLED=1` after staff are ready. Add only existing, individually controlled staff account emails to `LOOKUP_STAFF_EMAILS`. Never allowlist an address before its account is created. Lookup staff have queue access without general admin access.

### Local browser handoff

From this checkout on the staff workstation, run `python -m scripts.staff_lookup_runner ice` or
`python -m scripts.staff_lookup_runner eoir` using the project's Python environment and installed Chrome.
Beacon-4 opens the queue using its owner-only workstation credential. Staff can use the desktop shortcuts to start a shift.
The runner prepares one official lookup at a time and starts the official form's verification flow.
Complete the CAPTCHA and any site notices directly in Chrome. Review the official result, then save or defer it
in the Case Tracker queue. Saving or deferring advances to the next item. If a site rejects the session or changes its form,
complete the lookup manually in a regular browser or defer it. Keep the workstation attended while the runner is open.


### Burmese translations of USCIS responses

The dashboard uses the local `app/locales/uscis-my.json` catalog to translate standard USCIS status titles, description sentences, and history events into Burmese. Strict templates preserve returned dates, form identifiers, and receipt numbers. Translation does not call an external provider or change stored USCIS data, scheduling, notifications, or eligibility logic.

The Burmese view leads with the USIRD translation and retains the sanitized original English underneath. English mode shows the source wording. Unrecognized wording is retained in English with an explicit unavailable/partial-translation notice; it is never classified by a substring such as "approved". Extend the curated catalog and its regression checks when USCIS introduces new wording.

### Google Cloud Translation fallback for Burmese USCIS messages

Reviewed wording in `app/locales/uscis-my.json` is translated locally first. Unknown
USCIS status, description, and history text queues only on authenticated Burmese
dashboards belonging to paid users or admins. The web request never calls Google.
The separate `tracker-translation` service processes the encrypted SQLite cache
beside the main database. Source case records are not modified. Original English
always remains visible and is the authoritative source; machine translations are
labelled with Google Cloud Translation attribution.

Store `GOOGLE_TRANSLATE_API_KEY`, `USCIS_AUTO_TRANSLATE_ENABLED=1`, and
`USCIS_TRANSLATION_DAILY_CHAR_LIMIT=10000` in the owner-only
`~/.config/usird/cloud-translation.env`, outside the repository. The daily cap is
UTC, is atomically reserved before each API attempt, and cannot exceed 10,000
characters through configuration. All attempts count conservatively. The limit
only covers this integration, not other Google Cloud usage.

Only sanitized USCIS text nodes reach the queue. A-Numbers, receipt identifiers,
long numeric identifiers, email addresses, and URL text are replaced by tokens
before Google calls or cache storage; each viewer's identifiers are restored only
after validating tokens. Profile fields are never used. The same redacted wording
shares one cached translation across clients. Cache entries use the existing
DATA_KEY encryption and owner-only permissions. Failure or quota exhaustion keeps
English readable. Translation refresh replaces text blocks only, preserving forms
and tabs. Stop/disable the translation service and set the enable flag to 0 to
roll back Google usage; AAF and USCIS workers do not depend on this service.

### Prepared Beacon-4 four-window morning shift

Start manually on Beacon-4's logged-in desktop (no daily timer):

    cd ~/case-tracker-operator
    .venv/bin/python -m scripts.staff_shift_runner --workers 4 --service both

Staff complete official notices and every CAPTCHA in the ordinary visible Chrome
windows. The runner uses the existing owner-only `lookup-token` to authenticate
only to Case Tracker's lookup workspace. It does not require general admin access,
reuse client passwords, install CAPTCHA-solving extensions, or expose debugging
ports. It opens up to four windows, tiles them to the desktop, prefills saved
A-Numbers and country/nationality, waits for verified results, saves recognized
results, then moves to the next assignment. Use `--service eoir` or `--service ice`
for a focused pilot. Close windows or press Ctrl+C in the launcher to stop.

Assignments are atomically exclusive, globally capped at four, and expire after
15 minutes. Active CAPTCHA waits extend assignments every 45 seconds. Recovered
assignments reject old tokens. Changed identity/nationality, revoked ICE consent,
ineligible accounts, and expired assignments reject results. Retryable failures
preserve the prior successful result and timestamp and defer for 15 minutes.
Successful checks become due after 24 hours. EOIR uses the first saved A-Number
and its supplied nationality, as the existing queue does; ICE uses each opted-in
person's saved A-Number and country of birth. Never infer nationality from country
of birth or automatically opt anyone into ICE searches.

A local coordinator lock prevents staff launching a second four-window shift on
Beacon-4. Default shift duration is four hours; CAPTCHA wait is ten minutes.
Idle windows stop after one minute with no unassigned eligible work. Counts are
lookup jobs, not distinct clients: one person checked in EOIR and ICE is two jobs.
The old V2 roster is not imported or altered by this runner. Existing AAF/USCIS
workers, the V2 fleet split, and their schedules stay separate.

**Release readiness:** server queue and result validation are covered by synthetic
concurrency and authentication tests. Public EOIR/ICE input controls were inspected
on November 11, 2026. The EOIR nationality selector was corrected for the actual
`BURMA (MYANMAR) (BM)` option. A live attended pilot is still mandatory: prior
ACIS sessions sometimes rejected solved CAPTCHAs, and the current ICE result-page
layout has not been observed after a successful human solve. Unknown ICE layouts
are deferred, never called no-match. The strict result recognizer must be calibrated
against that pilot before treating ICE automatic saving as proven. No anti-bot
bypass or automatic CAPTCHA service is included. The existing manual staff pages
remain available for cases the official sites reject or the parser cannot recognize.

Beacon-4 preflight found Chrome, Playwright, and its private operator credential;
its existing virtual environment needs the tested `httpx==0.27.2` dependency from
`deploy/beacon4-shift-requirements.txt`. Install it only in the operator environment.

Deploy the matching server release and migrate its additive lease/attempt tables
before installing the workstation package. Back up the database encrypted first.
No client records should be checked until staff start the shift. The four-hour /
1,000-client timing is historical operator experience, not a measured guarantee
for the combined EOIR+ICE workload. This path saves results without initiating
new customer email/SMS alerts; customer-facing EOIR/ICE presentation is a separate
release from preparing the operator shift.
