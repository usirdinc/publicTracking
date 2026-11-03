"""Web app: sign-up, cases, instant AAF check, Square webhook, admin."""
import logging
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, db, security, square

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


templates.env.globals["status_text"] = status_text
templates.env.globals["price"] = config.PRICE


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
    return resp


def current_user(request: Request, cn):
    uid = request.session.get("uid")
    if not uid:
        return None
    return cn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def is_admin(user) -> bool:
    return bool(user and user["email"] in config.ADMIN_EMAILS)


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
    if "@" not in email or len(email) > 200:
        return redirect("/signup", "Enter a valid email address.", request)
    if len(password) < 10:
        return redirect("/signup", "Use a password of at least 10 characters.", request)
    if not agree:
        return redirect("/signup", "Please accept the Terms and Privacy Policy.", request)
    cn = get_db()
    n = cn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if n >= config.BETA_CAP:
        return redirect("/", "The beta is full. Please check back soon.", request)
    if cn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return redirect("/login", "That email already has an account. Please log in.", request)
    cur = cn.execute("INSERT INTO users(email,pw_hash,created_at) VALUES(?,?,?)",
                     (email, security.hash_password(password), db.iso(db.now())))
    request.session.clear()
    request.session["uid"] = cur.lastrowid
    return redirect("/app")


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
    return redirect("/app")


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
    cases = cn.execute("SELECT * FROM cases WHERE user_id=? ORDER BY id", (u["id"],)).fetchall()
    flash = request.session.pop("flash", None)
    return render(request, "dashboard.html", user=u, cases=cases, paid=db.is_paid(u),
                  checkout_url=config.SQUARE_CHECKOUT_URL, flash=flash,
                  paused=_paused(cn), admin=is_admin(u))


def _paused(cn) -> bool:
    pu = db.parse(db.get_setting(cn, "paused_until"))
    return bool(pu and pu > db.now())


def _can_use(u) -> bool:
    return db.is_paid(u) or is_admin(u)


@app.post("/cases")
def add_case(request: Request, label: str = Form(""), a_numbers: str = Form(...),
             receipt: str = Form(...), csrf: str = Form("")):
    cn = get_db()
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
    except ValueError as e:
        return redirect("/app", str(e), request)
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
            "INSERT INTO cases(user_id,label,a_numbers_enc,receipt_enc,a_last3,receipt_last4,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (u["id"], (label or "My case").strip()[:60], security.encrypt(";".join(anums)),
             security.encrypt(rcpt), anums[0][-3:], rcpt[-4:], db.iso(db.now())))
        db.enqueue(cn, cur.lastrowid, "new")
    return redirect("/app", "Case added. Checking with USCIS now; this usually takes under a minute.", request)


# A check counts as "in progress" while it runs or waits to run now. The next
# monthly check also sits in the queue, but it is not in progress.
ACTIVE_JOB = "(state='running' OR (state='queued' AND kind<>'scheduled'))"


def _own_case(cn, u, case_id):
    return cn.execute("SELECT * FROM cases WHERE id=? AND user_id=?", (case_id, u["id"])).fetchone()


@app.post("/cases/{case_id}/check")
def check_now(request: Request, case_id: int, csrf: str = Form("")):
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
        cn.execute("UPDATE cases SET uscis_next_at=NULL WHERE id=?", (case_id,))
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
    rows = cn.execute(
        "SELECT c.id, c.status, c.amount, c.checked_at, "
        f"(SELECT COUNT(*) FROM jobs j WHERE j.case_id=c.id AND {ACTIVE_JOB}) AS busy "
        "FROM cases c WHERE c.user_id=?", (u["id"],)).fetchall()
    out = []
    for r in rows:
        text, tone = status_text(r["status"], r["amount"])
        out.append({"id": r["id"], "busy": bool(r["busy"]), "text": "Checking…" if r["busy"] else text,
                    "tone": "pending" if r["busy"] else tone, "checked_at": r["checked_at"]})
    return {"cases": out}


@app.post("/account/delete")
def delete_account(request: Request, csrf: str = Form("")):
    cn = get_db()
    u = current_user(request, cn)
    if not u or not security.csrf_ok(request.session, csrf):
        return redirect("/app")
    cn.execute("DELETE FROM users WHERE id=?", (u["id"],))
    request.session.clear()
    return redirect("/", "Your account and all case data were deleted. Cancel your Square subscription to stop billing.", request)


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
    with db.tx(cn):
        outcome = square.handle_event(cn, square.parse_body(raw))
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
