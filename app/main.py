"""Web app: sign-up, cases, instant AAF check, Square webhook, admin."""
import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from urllib.parse import urlparse

from cryptography.fernet import InvalidToken
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.middleware.sessions import SessionMiddleware

from app import billing, config, db, security, square
from app.uscis_display import description_html, case_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("web")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY or "dev-only-not-secret",
                   same_site="lax", https_only=config.BASE_URL.startswith("https"),
                   max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(config.ROOT / "app" / "templates"))

STATUS_TEXT = {
    None: ("Checking…", "pending"),
    "DUE": ("Fee due", "due"),
    "NOT_DUE": ("Not due right now", "ok"),
    "EXPIRED": ("Payment period has expired", "warn"),
    "NOT_FOUND": ("USCIS could not find this case. Check the numbers.", "warn"),
    "NOT_PENDING": ("No payment needed", "ok"),
}


def status_text(status, amount=None):
    if status and status.startswith("ERROR"):
        return ("Could not check right now. We will retry.", "warn")
    text, tone = STATUS_TEXT.get(status, (status or "Checking…", "pending"))
    if status == "DUE" and amount:
        text = f"Fee due: ${amount}"
    return text, tone


AAF_BADGE = {"DUE": ("Fee due", "due"), "NOT_DUE": ("Not due", "ok"), "EXPIRED": ("Expired", "warn"),
             "NOT_FOUND": ("Not found", "warn"), "NOT_PENDING": ("Not needed", "ok")}


def aaf_badge(status):
    if not status:
        return ("Checking", "pending")
    if status.startswith("ERROR"):
        return ("Retrying", "warn")
    return AAF_BADGE.get(status, ("Status", "pending"))


def uscis_badge(text):
    t = (text or "").lower()
    if any(w in t for w in ("denied", "rejected", "terminated", "revoked", "returned", "evidence was requested")):
        return "warn"
    if any(w in t for w in ("approved", "card was", "produced", "delivered", "mailed", "oath")):
        return "ok"
    return "pending"


templates.env.globals["status_text"] = status_text
templates.env.globals["aaf_badge"] = aaf_badge
templates.env.globals["uscis_badge"] = uscis_badge
templates.env.filters["uscis_description"] = description_html
templates.env.globals["case_history"] = case_history
templates.env.globals["price"] = config.PRICE
templates.env.globals["max_cases"] = config.MAX_CASES_PER_USER


def get_db():
    cn = db.connect()
    db.init(cn)
    return cn


@app.on_event("startup")
def _startup():
    cn = get_db()
    cn.close()
    if not config.SECRET_KEY or not config.DATA_KEY:
        log.warning("SECRET_KEY or DATA_KEY missing: set them in .env before going live")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    if request.url.path == '/subscribe' and billing.enabled():
        cdn = 'https://sandbox.web.squarecdn.com' if config.SQUARE_ENV == 'sandbox' else 'https://web.squarecdn.com'
        pci = 'https://pci-connect.squareupsandbox.com' if config.SQUARE_ENV == 'sandbox' else 'https://pci-connect.squareup.com'
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' " + cdn + "; frame-src 'self' " + cdn +
            "; connect-src 'self' " + cdn + " " + pci + " https://o160250.ingest.sentry.io; "
            "style-src 'self' 'unsafe-inline' " + cdn + "; font-src 'self' "
            "https://square-fonts-production-f.squarecdn.com https://d1g145x70srn7h.cloudfront.net; "
            "img-src 'self' data: " + cdn + "; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
    if request.url.path in ('/subscribe', '/app') or request.url.path.startswith('/billing/'):
        resp.headers['Cache-Control'] = 'no-store, private'
    return resp


def current_user(request: Request, cn):
    uid = request.session.get("uid")
    if not uid:
        return None
    return cn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def is_admin(user) -> bool:
    return bool(user and user["email"] in config.ADMIN_EMAILS)


def is_lookup_staff(user):
    return bool(user and (is_admin(user) or user["email"] in config.LOOKUP_STAFF_EMAILS))


def render(request, name, **ctx):
    ctx.setdefault("user", None)
    if "flash" not in ctx:
        ctx["flash"] = request.session.pop("flash", None)
    ctx["csrf"] = security.csrf_token(request.session)
    ctx["request"] = request
    return templates.TemplateResponse(request, name, ctx)


def redirect(url, flash=None, request=None):
    if flash and request is not None:
        request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)


# ---- login throttling (per IP, in memory) ----
_attempts: dict[str, list[float]] = defaultdict(list)


def throttled(ip: str, limit=10, window=900) -> bool:
    t = time.time()
    _attempts[ip] = [x for x in _attempts[ip] if t - x < window]
    _attempts[ip].append(t)
    return len(_attempts[ip]) > limit


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


# ---------------- public pages ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cn = get_db()
    return render(request, "index.html", user=current_user(request, cn))


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return render(request, "terms.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/healthz")
def healthz():
    cn = get_db()
    q = cn.execute("SELECT COUNT(*) c FROM jobs WHERE state='queued'").fetchone()["c"]
    return {"ok": True, "queued": q}


# ---------------- accounts ----------------
@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return render(request, "signup.html")


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
           agree: str = Form(""), csrf: str = Form("")):
    if not security.csrf_ok(request.session, csrf):
        return redirect("/signup", "Your session expired. Please try again.", request)
    if throttled(client_ip(request)):
        return redirect("/signup", "Too many attempts. Try again in 15 minutes.", request)
    email = email.strip().lower()
    if len(email) > 200 or not security.valid_email(email):
        return redirect("/signup", "Enter a valid email address.", request)
    if len(password) < 10:
        return redirect("/signup", "Use a password of at least 10 characters.", request)
    if not agree:
        return redirect("/signup", "Please accept the Terms and Privacy Policy.", request)
    cn = get_db()
    n = cn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if n >= config.ACCOUNT_CAP:
        return redirect("/", "We are not accepting new registrations right now. Please check back soon.", request)
    if cn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return redirect("/login", "That email already has an account. Please log in.", request)
    cur = cn.execute("INSERT INTO users(email,pw_hash,created_at) VALUES(?,?,?)",
                     (email, security.hash_password(password), db.iso(db.now())))
    request.session.clear()
    request.session["uid"] = cur.lastrowid
    return redirect("/subscribe")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    if not security.csrf_ok(request.session, csrf):
        return redirect("/login", "Your session expired. Please try again.", request)
    if throttled(client_ip(request)):
        return redirect("/login", "Too many attempts. Try again in 15 minutes.", request)
    cn = get_db()
    u = cn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not u or not security.verify_password(u["pw_hash"], password):
        return redirect("/login", "Email or password is not correct.", request)
    request.session.clear()
    request.session["uid"] = u["id"]
    if u["email"] in config.LOOKUP_STAFF_EMAILS:
        return redirect("/staff/lookups")
    return redirect("/app" if _can_use(u) else "/subscribe")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/")


# ---------------- dashboard + cases ----------------
@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    cn = get_db()
    u = current_user(request, cn)
    if not u:
        return redirect("/login")
    if not _can_use(u):
        return redirect("/subscribe")
    cases = cn.execute("SELECT * FROM cases WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
    flash = request.session.pop("flash", None)
    return render(request, "dashboard.html", user=u, cases=cases, paid=db.is_paid(u),
                  checkout_url=config.SQUARE_CHECKOUT_URL, flash=flash,
                  paused=_paused(cn), admin=is_admin(u), billing_row=billing.enrollment(cn, u["id"]))


@app.get("/subscribe", response_class=HTMLResponse)
def subscribe(request: Request):
    """Step 2 of sign-up: an account can't be used until the subscription is paid."""
    cn = get_db()
    u = current_user(request, cn)
    if not u:
        return redirect("/login")
    if _can_use(u):
        return redirect("/app")
    flash = request.session.pop("flash", None)
    return render(request, "subscribe.html", user=u, checkout_url=config.SQUARE_CHECKOUT_URL,
                  lapsed=bool(u["paid_until"]), flash=flash, native=billing.enabled(),
                  billing_row=billing.enrollment(cn, u['id']),
                  square_app=config.SQUARE_APPLICATION_ID, square_location=config.SQUARE_LOCATION_ID,
                  square_sdk=('https://sandbox.web.squarecdn.com/v1/square.js' if config.SQUARE_ENV == 'sandbox'
                              else 'https://web.squarecdn.com/v1/square.js'))


@app.post('/billing/subscribe')
def native_subscribe(request: Request, csrf: str = Form(''), consent: str = Form(''),
                     source_id: str = Form(''), cardholder: str = Form('')):
    cn = get_db()
    try:
        u = current_user(request, cn)
        if not u:
            return JSONResponse({'message': 'Please log in again.'}, status_code=401)
        if not security.csrf_ok(request.session, csrf):
            return JSONResponse({'message': 'Your session expired. Refresh this page and try again.'}, status_code=403)
        if not billing.enabled():
            return JSONResponse({'message': 'Card checkout is temporarily unavailable.'}, status_code=503)
        row = billing.enrollment(cn, u['id'])
        # Recovery reuses an already authorized attempt; it cannot create a new subscription.
        recovery = row and row['state'] in ('uncertain', 'processing')
        if consent != '1' and not recovery:
            return JSONResponse({'message': 'Please authorize the monthly subscription before continuing.'}, status_code=400)
        if (not row or row['state'] in ('failed', 'ended')) and throttled('billing:' + str(u['id']), limit=8):
            return JSONResponse({'message': 'Too many payment attempts. Please wait 15 minutes and try again.'}, status_code=429)
        with billing.SquareBilling() as provider:
            state = provider.subscribe(cn, u, source_id.strip(), cardholder.strip())
        return {'state': state}
    except billing.BillingError as error:
        return JSONResponse({'message': str(error)}, status_code=400)
    finally:
        cn.close()


@app.get('/billing/invoice')
def native_invoice(request: Request):
    cn = get_db()
    try:
        u = current_user(request, cn)
        if not u:
            return redirect('/login')
        with billing.SquareBilling() as provider:
            url = provider.invoice_url(cn, u['id'])
        parsed = urlparse(url or '')
        host = parsed.hostname or ''
        if parsed.scheme == 'https' and (host == 'squareup.com' or host.endswith('.squareup.com')) and not parsed.username and not parsed.password:
            return redirect(url)
        return redirect('/subscribe', 'Square has not issued an outstanding invoice yet. Please wait for confirmation or contact USIRD.', request)
    except (billing.BillingError, KeyError, TypeError):
        return redirect('/subscribe', 'The invoice is temporarily unavailable. Please try again shortly.', request)
    finally:
        cn.close()


@app.post('/billing/cancel')
def native_cancel(request: Request, csrf: str = Form('')):
    cn = get_db()
    try:
        u = current_user(request, cn)
        if not u or not security.csrf_ok(request.session, csrf):
            return redirect('/app')
        with billing.SquareBilling() as provider:
            provider.cancel(cn, u['id'])
        return redirect('/app', 'Cancellation requested. Access continues through your paid period.', request)
    except billing.BillingError:
        return redirect('/app', 'Square could not confirm cancellation. Please try again or contact USIRD.', request)
    finally:
        cn.close()


@app.get("/account/paid")
def account_paid(request: Request):
    cn = get_db()
    u = current_user(request, cn)
    return JSONResponse({"paid": bool(u and _can_use(u))},
                        headers={"Cache-Control": "no-store, private"})


def _paused(cn) -> bool:
    pu = db.parse(db.get_setting(cn, "paused_until"))
    return bool(pu and pu > db.now())


def _can_use(u) -> bool:
    return db.is_paid(u) or is_admin(u)


def _client_details(first_name, last_name, dob, phone, email):
    first_name, last_name = (" ".join(name.split()) for name in (first_name, last_name))
    if not first_name or not last_name or max(len(first_name), len(last_name)) > 100:
        raise ValueError("Enter a first and last name of up to 100 characters each.")
    try:
        birth_date = date.fromisoformat(dob)
    except ValueError:
        raise ValueError("Enter a valid date of birth.") from None
    if birth_date.isoformat() != dob or birth_date > date.today():
        raise ValueError("Enter a valid date of birth.")
    phone = security.normalize_contact_phone(phone)
    email = email.strip()
    if not security.valid_email(email):
        raise ValueError("Enter a valid contact email.")
    return {"first_name": first_name, "last_name": last_name, "dob": dob,
            "phone": phone, "email": email}


def _case_intake(request, user, *, error=None, **values):
    response = render(request, "case_new.html", user=user, error=error,
                      details=values, label=values.get("label", ""),
                      a_numbers=values.get("a_numbers", ""), receipt=values.get("receipt", ""))
    response.headers["Cache-Control"] = "no-store, private"
    if error:
        response.status_code = 422
    return response


@app.get("/cases/new", response_class=HTMLResponse)
def new_case(request: Request):
    cn = get_db()
    try:
        user = current_user(request, cn)
        if not user:
            return redirect("/login")
        if not _can_use(user):
            return redirect("/subscribe")
        return _case_intake(request, user, email=user["email"])
    finally:
        cn.close()


@app.post("/cases")
def add_case(request: Request, label: str = Form(""), a_numbers: str = Form(...),
             receipt: str = Form(...), nationality: str = Form(""), csrf: str = Form(""),
             first_name: str = Form(""), last_name: str = Form(""), dob: str = Form(""),
             phone: str = Form(""), email: str = Form("")):
    cn = get_db()
    try:
        u = current_user(request, cn)
        if not u:
            return redirect("/login")
        if not security.csrf_ok(request.session, csrf):
            return redirect("/app", "Your session expired. Please try again.", request)
        if not _can_use(u):
            return redirect("/app", "Please subscribe first to add a case.", request)
        try:
            anums = security.normalize_a_numbers(a_numbers)
            rcpt = security.normalize_receipt(receipt)
            details = _client_details(first_name, last_name, dob, phone, email)
        except ValueError as e:
            return _case_intake(request, u, error=str(e), label=label, a_numbers=a_numbers,
                                receipt=receipt, first_name=first_name, last_name=last_name,
                                dob=dob, phone=phone, email=email)
        total = cn.execute("SELECT COUNT(*) c FROM cases WHERE user_id=?", (u["id"],)).fetchone()["c"]
        if total >= config.MAX_CASES_PER_USER:
            return redirect("/app", f"You can track up to {config.MAX_CASES_PER_USER} cases.", request)
        since = db.iso(db.now() - timedelta(days=1))
        today = cn.execute("SELECT COUNT(*) c FROM cases WHERE user_id=? AND created_at>?",
                           (u["id"], since)).fetchone()["c"]
        if today >= config.NEW_CASES_PER_DAY:
            return redirect("/app", "You have added the most cases allowed today. Try again tomorrow.", request)
        with db.tx(cn):
            cur = cn.execute(
                "INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at,nationality,client_details_enc) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (u["id"], (label or "My case").strip()[:60], security.encrypt(";".join(anums)),
                 security.encrypt(rcpt), anums[0][-3:], rcpt[-4:], db.iso(db.now()),
                 (re.sub(r"[^A-Za-z ()'.-]", "", nationality).strip()[:60] or None) if config.EOIR_ENABLED else None,
                 security.encrypt(json.dumps(details))))
            db.enqueue(cn, cur.lastrowid, "new")
        return redirect("/app", "Case added. Your first asylum fee and USCIS status checks are queued.", request)
    finally:
        cn.close()


# A check counts as "in progress" while it runs or waits to run now. The next
# monthly check also sits in the queue, but it is not in progress.
ACTIVE_JOB = "(state='running' OR (state='queued' AND kind<>'scheduled'))"


def _own_case(cn, u, case_id):
    return cn.execute("SELECT * FROM cases WHERE id=? AND user_id=?", (case_id, u["id"])).fetchone()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


@app.get("/cases/{case_id}/details", response_class=HTMLResponse)
@app.post("/cases/{case_id}/details", response_class=HTMLResponse)
def case_details(request: Request, case_id: int, csrf: str = Form(""),
                 first_name: str = Form(""), last_name: str = Form(""), dob: str = Form(""),
                 phone: str = Form(""), email: str = Form("")):
    with closing(get_db()) as cn:
        user = current_user(request, cn)
        if not user:
            return redirect("/login")
        if not _can_use(user):
            return redirect("/subscribe")
        case = _own_case(cn, user, case_id)
        if not case:
            return Response(status_code=404)
        error = None
        if request.method == "POST":
            if not security.csrf_ok(request.session, csrf):
                return Response(status_code=403)
            try:
                details = _client_details(first_name, last_name, dob, phone, email)
            except ValueError as exc:
                error = str(exc)
                details = {"first_name": first_name[:100], "last_name": last_name[:100],
                           "dob": dob[:10], "phone": phone[:40], "email": email[:254]}
            else:
                saved = cn.execute("UPDATE cases SET client_details_enc=? WHERE id=? AND user_id=?",
                                   (security.encrypt(json.dumps(details)), case_id, user["id"]))
                if saved.rowcount != 1:
                    return Response(status_code=404)
                return redirect("/app", "Case details saved.", request)
        else:
            try:
                encrypted = case["client_details_enc"]
                details = json.loads(security.decrypt(encrypted)) if encrypted else {"email": user["email"]}
            except (InvalidToken, ValueError):
                return Response("Saved case details are temporarily unavailable.", status_code=503)
        response = render(request, "case_details.html", user=user, case=case, details=details,
                          error=error, admin=is_admin(user))
        response.headers["Cache-Control"] = "no-store, private"
        return response


@app.get("/cases/{case_id}/ice", response_class=HTMLResponse)
@app.post("/cases/{case_id}/ice", response_class=HTMLResponse)
def ice_lookup(request: Request, case_id: int, person: int = Form(-1),
               country: str = Form(""), csrf: str = Form(""), action: str = Form(""), staff: str = Form("")):
    with closing(get_db()) as cn:
        user = current_user(request, cn)
        if not user:
            return redirect("/login")
        if not _can_use(user):
            return redirect("/subscribe")
        case = _own_case(cn, user, case_id)
        if not case:
            return Response(status_code=404)
        if request.method == "POST" and not security.csrf_ok(request.session, csrf):
            return Response(status_code=403)
        prepared = None
        message = None
        try:
            numbers = list(dict.fromkeys(security.decrypt(case["a_numbers_enc"]).split(";")))
            if request.method == "POST":
                country = " ".join(country.split())
                if not 0 <= person < len(numbers) or len(country) > 100 or action not in ("save", "prepare"):
                    return Response("Invalid ICE lookup details.", status_code=400)
                if action == "prepare" and not country:
                    return Response("Enter the country of birth before preparing a lookup.", status_code=400)
                with db.tx(cn):
                    current = _own_case(cn, user, case_id)
                    if not current:
                        return Response(status_code=404)
                    encrypted = current["ice_birth_countries_enc"]
                    countries = json.loads(security.decrypt(encrypted)) if encrypted else {}
                    previous_country = countries.get(numbers[person], "")
                    if country:
                        countries[numbers[person]] = country
                    else:
                        countries.pop(numbers[person], None)
                    cn.execute("UPDATE cases SET ice_birth_countries_enc=? WHERE id=? AND user_id=?",
                               (security.encrypt(json.dumps(countries)) if countries else None, case_id, user["id"]))
                    if country != previous_country or (config.ICE_STAFF_ENABLED and staff != "1"):
                        cn.execute("DELETE FROM ice_staff_checks WHERE case_id=? AND person=?", (case_id, person))
                    if config.ICE_STAFF_ENABLED and country and staff == "1":
                        cn.execute("INSERT OR IGNORE INTO ice_staff_checks(case_id,person) VALUES(?,?)", (case_id, person))
                message = "Country of birth saved." if country else "Country of birth cleared."
                if action == "prepare":
                    prepared = {"number": numbers[person].removeprefix("A"), "country": country}
            else:
                encrypted = case["ice_birth_countries_enc"]
                countries = json.loads(security.decrypt(encrypted)) if encrypted else {}
        except (InvalidToken, ValueError):
            return Response("Saved lookup details are temporarily unavailable.", status_code=503)
        staff_checks = {row["person"]: row for row in cn.execute(
            "SELECT * FROM ice_staff_checks WHERE case_id=?", (case_id,)
        )}
        people = []
        for index, number in enumerate(numbers):
            check = staff_checks.get(index)
            result = None
            if check and check["result_enc"]:
                try:
                    result = json.loads(security.decrypt(check["result_enc"]))
                except (InvalidToken, ValueError):
                    pass
            people.append({"last3": number[-3:], "country": countries.get(number, ""),
                           "staff": check is not None, "result": result,
                           "checked_at": check["checked_at"] if check else None})
        response = render(request, "ice_lookup.html", user=user, case=case, people=people,
                          prepared=prepared, message=message, admin=is_admin(user),
                          ice_staff_enabled=config.ICE_STAFF_ENABLED)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


@app.post("/cases/{case_id}/check")
def check_now(request: Request, case_id: int, csrf: str = Form("")):
    resp = _check_now(request, case_id, csrf)
    if _wants_json(request) and isinstance(resp, RedirectResponse):
        msg = request.session.pop("flash", None)
        return JSONResponse({"ok": msg == "Checking now…", "message": msg})
    return resp


def _check_now(request: Request, case_id: int, csrf: str):
    cn = get_db()
    u = current_user(request, cn)
    if not u:
        return redirect("/login")
    if not security.csrf_ok(request.session, csrf):
        return redirect("/app", "Your session expired. Please try again.", request)
    c = _own_case(cn, u, case_id)
    if not c or not _can_use(u):
        return redirect("/app")
    since = db.iso(db.now() - timedelta(days=1))
    pending = cn.execute(f"SELECT COUNT(*) c FROM jobs WHERE case_id=? AND {ACTIVE_JOB}",
                         (case_id,)).fetchone()["c"]
    if pending:
        return redirect("/app", "A check for this case is already in progress.", request)
    used = cn.execute("SELECT COUNT(*) c FROM jobs WHERE case_id=? AND kind='instant' AND created_at>?",
                      (case_id, since)).fetchone()["c"]
    if used >= config.INSTANT_CHECKS_PER_CASE_PER_DAY and not is_admin(u):
        return redirect("/app", "You can run one instant check per case per day. We also check automatically.", request)
    with db.tx(cn):
        db.enqueue(cn, case_id, "instant")
        cn.execute("UPDATE cases SET uscis_next_at=NULL, uscis_error=NULL WHERE id=?", (case_id,))
    return redirect("/app", "Checking now…", request)


@app.post("/cases/{case_id}/delete")
def delete_case(request: Request, case_id: int, csrf: str = Form("")):
    cn = get_db()
    u = current_user(request, cn)
    if not u or not security.csrf_ok(request.session, csrf):
        return redirect("/app")
    cn.execute("DELETE FROM cases WHERE id=? AND user_id=?", (case_id, u["id"]))
    return redirect("/app", "Case deleted, including its numbers and history.", request)


@app.get("/cases/status")
def cases_status(request: Request):
    """Polled by the dashboard while a check is running."""
    cn = get_db()
    u = current_user(request, cn)
    if not u:
        return JSONResponse({"error": "login"}, status_code=401)
    rows = cn.execute("SELECT id, status, amount, checked_at FROM cases WHERE user_id=?", (u["id"],)).fetchall()
    out = []
    for r in rows:
        text, tone = status_text(r["status"], r["amount"])
        item = {"id": r["id"], "busy": False, "text": text, "tone": tone, "checked_at": r["checked_at"]}
        j = cn.execute(f"SELECT * FROM jobs WHERE case_id=? AND {ACTIVE_JOB} ORDER BY state='running' DESC, id LIMIT 1",
                       (r["id"],)).fetchone()
        if j:
            if j["state"] == "running":
                pct, stage = j["progress"] or 10, j["stage"] or "Checking with USCIS"
            else:
                ahead = cn.execute(
                    "SELECT COUNT(*) n FROM jobs WHERE state='running' OR (state='queued' AND run_after<=? "
                    "AND (priority<? OR (priority=? AND (run_after<? OR (run_after=? AND id<?)))))",
                    (db.iso(db.now()), j["priority"], j["priority"], j["run_after"], j["run_after"], j["id"])
                ).fetchone()["n"]
                if j["run_after"] > db.iso(db.now()):
                    pct, stage = 5, "Retrying shortly"
                else:
                    pct, stage = 5, ("Waiting in line" + (f" ({ahead} ahead of you)" if ahead else ""))
            item.update(busy=True, text=f"Checking… {pct}%", tone="pending", progress=pct, stage=stage)
        out.append(item)
    return {"cases": out}


@app.post("/account/delete")
def delete_account(request: Request, csrf: str = Form("")):
    cn = get_db()
    u = current_user(request, cn)
    if not u or not security.csrf_ok(request.session, csrf):
        return redirect("/app")
    with db.tx(cn):
        row = billing.enrollment(cn, u['id'])
        if row and row['state'] in ('processing', 'uncertain'):
            return redirect('/subscribe', 'Please finish confirming your subscription before deleting your account, so billing can be stopped safely.', request)
        needs_cancel = bool(row and row['subscription_id'] and row['state'] != 'ended' and not row['cancel_scheduled'])
        if not needs_cancel:
            cn.execute('DELETE FROM users WHERE id=?', (u['id'],))
    if needs_cancel:
        try:
            with billing.SquareBilling() as provider:
                provider.cancel(cn, u['id'])
        except billing.BillingError:
            return redirect('/app', 'Square could not confirm cancellation. Please try again before deleting your account.', request)
        with db.tx(cn):
            row = billing.enrollment(cn, u['id'])
            if row and row['state'] in ('processing', 'uncertain'):
                return redirect('/subscribe', 'Please finish confirming your subscription before deleting your account.', request)
            cn.execute('DELETE FROM users WHERE id=?', (u['id'],))
    request.session.clear()
    message = 'Your account and all case data were deleted.'
    if not row:
        message += ' Cancel any existing Square subscription to stop billing.'
    return redirect('/', message, request)


# ---------------- Square webhook ----------------
@app.post("/webhooks/square")
async def square_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-square-hmacsha256-signature", "")
    url = config.BASE_URL + "/webhooks/square"
    if not square.verify_signature(raw, sig, url):
        log.warning("square webhook: bad signature")
        return Response(status_code=403)
    cn = get_db()
    try:
        with db.tx(cn):
            outcome = square.handle_event(cn, square.parse_body(raw))
    except square.PaymentDeferred:
        # Roll back deduplication too: Square must retry after creation completes.
        return Response(status_code=503)
    finally:
        cn.close()
    log.info("square webhook: %s", outcome)
    return {"ok": True}


# ---------------- admin ----------------
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    cn = get_db()
    u = current_user(request, cn)
    if not is_admin(u):
        return redirect("/login")
    users = cn.execute(
        "SELECT u.id,u.email,u.created_at,u.paid_until,u.subscription_status,"
        "(SELECT COUNT(*) FROM cases c WHERE c.user_id=u.id) n_cases FROM users u ORDER BY u.id DESC").fetchall()
    stats = {r["state"]: r["n"] for r in cn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
    last = cn.execute("SELECT status, COUNT(*) n FROM checks WHERE checked_at>? GROUP BY status",
                      (db.iso(db.now() - timedelta(days=1)),)).fetchall()
    return render(request, "admin.html", user=u, users=users, stats=stats, last=last,
                  paused_until=db.get_setting(cn, "paused_until"), pause_reason=db.get_setting(cn, "pause_reason"),
                  flash=request.session.pop("flash", None))


@app.post("/admin/users/{uid}/grant")
def admin_grant(request: Request, uid: int, days: int = Form(31), csrf: str = Form("")):
    cn = get_db()
    u = current_user(request, cn)
    if not is_admin(u) or not security.csrf_ok(request.session, csrf):
        return redirect("/login")
    t = cn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if t:
        base = max(db.now(), db.parse(t["paid_until"]) or db.now())
        cn.execute("UPDATE users SET paid_until=? WHERE id=?", (db.iso(base + timedelta(days=days)), uid))
    return redirect("/admin", f"Granted {days} days to user {uid}.", request)


@app.post("/admin/resume")
def admin_resume(request: Request, csrf: str = Form("")):
    cn = get_db()
    u = current_user(request, cn)
    if not is_admin(u) or not security.csrf_ok(request.session, csrf):
        return redirect("/login")
    db.set_setting(cn, "paused_until", None)
    return redirect("/admin", "Checks resumed.", request)


# ---------------- Staff lookup workspace ----------------
LOOKUP_COOKIE = "__Host-usird-lookup"
LOOKUP_SESSION_SECONDS = 8 * 60 * 60


def _lookup_signer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="usird-beacon-lookup-v1")


def _lookup_authorized(request, user):
    if is_lookup_staff(user):
        return True
    cookie = request.cookies.get(LOOKUP_COOKIE)
    expected = config.LOOKUP_MACHINE_TOKEN_SHA256
    if not cookie or not expected or not config.SECRET_KEY:
        return False
    try:
        fingerprint = _lookup_signer().loads(cookie, max_age=LOOKUP_SESSION_SECONDS)
    except BadData:
        return False
    return isinstance(fingerprint, str) and hmac.compare_digest(fingerprint, expected)


@app.post("/staff/lookups/session")
def lookup_machine_session(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = config.LOOKUP_MACHINE_TOKEN_SHA256
    if not config.SECRET_KEY or not expected or not config.BASE_URL.startswith("https://"):
        return Response(status_code=503)
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    if scheme.lower() != "bearer" or not hmac.compare_digest(fingerprint, expected):
        return Response(status_code=403)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(LOOKUP_COOKIE, _lookup_signer().dumps(fingerprint),
                        max_age=LOOKUP_SESSION_SECONDS, secure=True, httponly=True, samesite="strict")
    return response


@app.get("/staff/lookups", response_class=HTMLResponse)
def staff_lookups(request: Request):
    with closing(get_db()) as cn:
        user = current_user(request, cn)
        if not _lookup_authorized(request, user):
            return redirect("/login")
        due = []
        if config.ICE_STAFF_ENABLED:
            rows = cn.execute(
                """SELECT i.person, i.version, c.id, c.label, c.a_numbers_enc, c.ice_birth_countries_enc
                   FROM ice_staff_checks i JOIN cases c ON c.id=i.case_id
                   JOIN users u ON u.id=c.user_id
                   WHERE (i.next_at IS NULL OR i.next_at<=?) AND u.paid_until>?
                   ORDER BY i.next_at, i.case_id, i.person LIMIT 25""",
                (db.iso(db.now()), db.iso(db.now())),
            )
            for row in rows:
                try:
                    numbers = list(dict.fromkeys(security.decrypt(row["a_numbers_enc"]).split(";")))
                    number = numbers[row["person"]]
                    country = json.loads(security.decrypt(row["ice_birth_countries_enc"])).get(number)
                except (InvalidToken, ValueError, IndexError, AttributeError):
                    continue
                if country:
                    due.append({"id": row["id"], "person": row["person"], "label": row["label"],
                                "number": number.removeprefix("A"), "country": country, "version": row["version"]})
        response = render(request, "staff_lookups.html", user=user, due=due,
                          ice_enabled=config.ICE_STAFF_ENABLED, eoir_enabled=config.EOIR_ENABLED,
                          admin=is_admin(user))
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


@app.post("/staff/lookups/ice/{case_id}/{person}")
def staff_ice_save(request: Request, case_id: int, person: int, csrf: str = Form(""),
                   status: str = Form(""), facility: str = Form(""), version: str = Form("")):
    with closing(get_db()) as cn:
        user = current_user(request, cn)
        if not config.ICE_STAFF_ENABLED or not _lookup_authorized(request, user):
            return Response(status_code=403)
        if not security.csrf_ok(request.session, csrf):
            return Response(status_code=403)
        facility = " ".join(facility.split())
        if status not in ("listed", "no_match", "retry") or len(facility) > 160:
            return Response("Invalid ICE lookup result.", status_code=400)
        now = db.iso(db.now())
        with db.tx(cn):
            row = cn.execute(
                """SELECT i.next_at, i.version, u.paid_until FROM ice_staff_checks i
                   JOIN cases c ON c.id=i.case_id JOIN users u ON u.id=c.user_id
                   WHERE i.case_id=? AND i.person=?""", (case_id, person)
            ).fetchone()
            if not row or row["version"] != version or not db.is_paid(row) or (row["next_at"] and row["next_at"] > now):
                return redirect("/staff/lookups", "This lookup is no longer due.", request)
            if status == "retry":
                cn.execute("UPDATE ice_staff_checks SET next_at=? WHERE case_id=? AND person=?",
                           (db.in_(days=1), case_id, person))
            else:
                result = security.encrypt(json.dumps({
                    "status": status, "facility": facility if status == "listed" else "",
                }))
                cn.execute("""UPDATE ice_staff_checks SET result_enc=?,checked_at=?,next_at=?
                              WHERE case_id=? AND person=?""",
                           (result, now, db.in_(days=1), case_id, person))
        message = "Deferred until tomorrow." if status == "retry" else "ICE result saved."
        return redirect("/staff/lookups", message, request)


# ---------------- EOIR: staff-assisted lookups ----------------
# ACIS rejects captchas solved in an automated browser, so a staff member looks
# each case up by hand and pastes the result page here; we parse and store it.


def _eoir_due(cn, limit=50):
    rows = cn.execute(
        "SELECT c.*, u.email, u.paid_until FROM cases c JOIN users u ON u.id=c.user_id "
        "WHERE c.nationality IS NOT NULL AND (c.eoir_next_at IS NULL OR c.eoir_next_at<=?) "
        "ORDER BY c.eoir_next_at IS NOT NULL, c.eoir_next_at, c.id LIMIT ?",
        (db.iso(db.now()), limit)).fetchall()
    return [r for r in rows if db.is_paid(r) or r["email"] in config.ADMIN_EMAILS]


def eoir_summary(status, fields_json):
    if not status:
        return None
    if status == "NO_CASE":
        return "No immigration court case found for this A-Number."
    f = json.loads(fields_json or "{}")
    parts = []
    if f.get("hearing_date"):
        h = f"Next hearing {f['hearing_date']}"
        if f.get("hearing_time"):
            h += f" at {f['hearing_time']}"
        if f.get("hearing_format"):
            h += f" ({f['hearing_format'].lower()})"
        parts.append(h)
    elif f.get("hearing"):
        parts.append(f["hearing"])
    if f.get("judge"):
        parts.append(f"Judge {f['judge']}")
    if f.get("decision"):
        parts.append(f"Decision: {f['decision']}")
    return ". ".join(parts) or "Case found in immigration court."


templates.env.globals["eoir_summary"] = eoir_summary
templates.env.globals["eoir_enabled"] = config.EOIR_ENABLED


@app.post("/cases/{case_id}/eoir")
def save_eoir_nationality(request: Request, case_id: int, nationality: str = Form(""),
                          csrf: str = Form("")):
    with closing(get_db()) as cn:
        user = current_user(request, cn)
        if not config.EOIR_ENABLED or not _can_use(user) or not security.csrf_ok(request.session, csrf):
            return Response(status_code=403)
        nationality = " ".join(nationality.split())
        if len(nationality) > 60:
            return Response("Enter a nationality of up to 60 characters.", status_code=400)
        with db.tx(cn):
            case = _own_case(cn, user, case_id)
            if not case:
                return Response(status_code=404)
            if (nationality or None) != case["nationality"]:
                cn.execute("""UPDATE cases SET nationality=?,eoir_next_at=NULL,eoir_status=NULL,
                              eoir_fields=NULL,eoir_checked_at=NULL WHERE id=? AND user_id=?""",
                           (nationality or None, case_id, user["id"]))
        return redirect("/app", "EOIR lookup details saved.", request)


@app.get("/admin/eoir", response_class=HTMLResponse)
def admin_eoir(request: Request):
    cn = get_db()
    u = current_user(request, cn)
    if not _lookup_authorized(request, u):
        return redirect("/login")
    if not config.EOIR_ENABLED:
        return redirect("/admin", "EOIR lookups are turned off (EOIR_ENABLED=0).", request)
    due = []
    for c in _eoir_due(cn):
        due.append({"id": c["id"], "label": c["label"], "email": c["email"], "nationality": c["nationality"],
                    "a_number": security.decrypt(c["a_numbers_enc"]).split(";")[0],
                    "last": eoir_summary(c["eoir_status"], c["eoir_fields"])})
    resp = render(request, "admin_eoir.html", user=u, due=due, flash=request.session.pop("flash", None))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/admin/eoir/{case_id}")
def admin_eoir_save(request: Request, case_id: int, page_text: str = Form(""), action: str = Form("save"),
                    csrf: str = Form(""), expected_nationality: str = Form("")):
    from tracker.eoir import NO_CASE_TEXT, extract_fields
    from app import mailer
    cn = get_db()
    u = current_user(request, cn)
    if not _lookup_authorized(request, u) or not security.csrf_ok(request.session, csrf):
        return redirect("/login")
    if not config.EOIR_ENABLED:
        return redirect("/admin")
    c = cn.execute("SELECT c.*, u.email, u.paid_until FROM cases c JOIN users u ON u.id=c.user_id WHERE c.id=?",
                   (case_id,)).fetchone()
    if not c or not c["nationality"] or not _can_use(c):
        return redirect("/admin/eoir", "Case not found or ineligible for staff checks.", request)
    if (expected_nationality or not is_admin(u)) and expected_nationality != c["nationality"]:
        return redirect("/admin/eoir", "The nationality changed. Reload the queue before continuing.", request)
    if c["eoir_next_at"] and c["eoir_next_at"] > db.iso(db.now()):
        return redirect("/admin/eoir", "This case is no longer due.", request)
    if action == "skip":
        cn.execute("UPDATE cases SET eoir_next_at=? WHERE id=?", (db.in_(days=1), case_id))
        return redirect("/admin/eoir", f"Skipped case {case_id} until tomorrow.", request)
    text = page_text.strip()
    if NO_CASE_TEXT.lower() in text.lower():
        status, fields = "NO_CASE", {}
    else:
        fields = extract_fields(text)
        if not fields:
            return redirect("/admin/eoir", "Couldn't read that. Paste the whole result page (Ctrl+A, Ctrl+C).", request)
        status = "CASE_FOUND"
    fields.pop("name", None)  # not needed for alerts
    old = eoir_summary(c["eoir_status"], c["eoir_fields"])
    new_json = json.dumps(fields, sort_keys=True)
    saved = cn.execute("""UPDATE cases SET eoir_status=?,eoir_fields=?,eoir_checked_at=?,eoir_next_at=?
                           WHERE id=? AND nationality=? AND eoir_next_at IS ?""",
                       (status, new_json, db.iso(db.now()), db.in_(days=config.EOIR_RECHECK_DAYS),
                        case_id, c["nationality"], c["eoir_next_at"]))
    if saved.rowcount != 1:
        return redirect("/admin/eoir", "The lookup changed while you were saving. Reload the queue.", request)
    new = eoir_summary(status, new_json)
    if old != new:
        mailer.send(c["email"], f"Immigration court update for {c['label']}",
                    f"The immigration court (EOIR) information for \"{c['label']}\" "
                    f"(A-Number ending {c['a_last3']}) {'changed' if old else 'is now available'}.\n\n"
                    f"Now: {new}\n" + (f"Before: {old}\n" if old else "") +
                    f"\nSee details: {config.BASE_URL}/app\n\n"
                    "Always confirm with the court or your attorney. This is not legal advice.\n")
    return redirect("/admin/eoir", f"Saved case {case_id}: {new}", request)
