"""NOTE: ACIS rejects captchas solved in a script-driven browser, so subscriber
EOIR checks are staff-assisted (/admin/eoir); only extract_fields() and
NO_CASE_TEXT are used in production. The browser code below is kept for reference.

EOIR ACIS court case lookup (acis.eoir.justice.gov), ported from USIRD v2
automation/eoir/eoir_check.py (sync Playwright).

hCaptcha is solved by a person watching the VPS screen through noVNC. We never
use captcha-solving services or extensions; this module only waits for the human.
"""
from __future__ import annotations

import os
import re
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

ACIS_URL = "https://acis.eoir.justice.gov/en/"
NO_CASE_TEXT = "No case found for this A-Number."
CAPTCHA_TIMEOUT = int(os.environ.get("EOIR_CAPTCHA_TIMEOUT", "180"))
# Known captcha-solver extension; refuse to run if it is active in the profile.
BLOCKED_EXTENSION_IDS = {"dknlfmjaanfblgfdfebhijalfmhmjjjo"}


@dataclass
class EoirResult:
    status: str                      # CASE_FOUND | NO_CASE | CAPTCHA_TIMEOUT | ERROR
    fields: dict = field(default_factory=dict)
    note: str = ""
    captcha: bool = False            # a person had to solve a captcha


def profile_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get("OPERATOR_PROFILE", os.path.join(root, "data", "operator-profile"))


def _lock_alive(lock: str) -> bool:
    """SingletonLock is a symlink to '<host>-<pid>'. True if that Chrome still runs."""
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[1])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def _check_profile(path: str):
    if not os.path.isdir(path):
        raise RuntimeError(f"Operator profile not found at {path}. Run scripts/operator_browser.sh and sign in first.")
    lock = os.path.join(path, "SingletonLock")
    if os.path.lexists(lock) and not _lock_alive(lock):
        # Left behind when Chrome was killed; Chrome would refuse to start otherwise.
        for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                os.unlink(os.path.join(path, f))
            except FileNotFoundError:
                pass
    if os.path.lexists(lock):
        raise RuntimeError("The operator Chrome window is still open. Close it (or run: pkill -f operator-profile) and try again.")
    for ext in BLOCKED_EXTENSION_IDS:
        if os.path.isdir(os.path.join(path, "Default", "Extensions", ext)):
            raise RuntimeError(f"Captcha-solver extension {ext} is installed in the profile. Remove it first.")


def _chrome_binary() -> str:
    import shutil
    for name in ("google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("Google Chrome is not installed: sudo .venv/bin/python -m playwright install --with-deps chrome")


@contextmanager
def operator_page(port: int = 9222):
    """Start the real Google Chrome ourselves (same as opening it by hand) with the
    signed-in operator profile on display :99, then attach to it. Playwright does
    not launch the browser, so none of its automation flags are set."""
    import subprocess
    import urllib.request
    from playwright.sync_api import sync_playwright
    path = profile_dir()
    _check_profile(path)
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":99"))
    proc = subprocess.Popen(
        [_chrome_binary(), f"--user-data-dir={path}", f"--remote-debugging-port={port}",
         "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
         "--disable-dev-shm-usage", "--start-maximized", ACIS_URL],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("Chrome exited on start (is the profile already open?)")
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome did not open its control port")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                yield page
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()


def _dismiss_accept(page, wait_ms=8000):
    sels = ['button:has-text("Accept")', 'button:has-text("I Accept")', 'button:has-text("Continue")',
            'button:has-text("OK")', 'a:has-text("Accept")']
    end = time.monotonic() + wait_ms / 1000
    while time.monotonic() < end:
        for s in sels:
            b = page.locator(s).first
            try:
                if b.is_visible():
                    b.click(); page.wait_for_timeout(1200)
                    return True
            except Exception:
                pass
        page.wait_for_timeout(300)
    return False


def _to_form(page):
    if "caseInformation" in page.url or "acis.eoir" not in page.url:
        page.goto(ACIS_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('button:has-text("Accept"), input[type="number"]', timeout=15000)
        except Exception:
            pass
    _dismiss_accept(page, 4000)
    page.wait_for_selector('input[type="number"]', timeout=15000)
    page.wait_for_timeout(500)


def extract_fields(text: str) -> dict:
    out = {}
    m = re.search(r'Name:\s*(.+?)(?:\s*\||\s*A-Number)', text)
    if m: out["name"] = m.group(1).strip()
    m = re.search(r'Docket Date:\s*([\d/]+)', text)
    if m: out["docket_date"] = m.group(1)
    if re.search(r'no\s+(?:future|upcoming|scheduled)\s+hearings?|no\s+hearings?\s+(?:are\s+)?scheduled', text, re.I):
        out["hearing"] = "No hearing scheduled"
    else:
        date_re = r'([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})'
        time_re = r'(\d+:\d+\s*[AaPp][Mm])'
        fmt_re = r'(IN[\s-]?PERSON|TELEPHONIC(?:[\s-]BASED)?|VIDEO(?:[\s-]BASED)?|INTERNET(?:[\s-]BASED)?)'
        m = re.search(r'(?:upcoming|next)\s+(\w+)\s+hearing\s+is\s+' + fmt_re + r'\s+on\s+' + date_re + r'\s+at\s+' + time_re, text, re.I)
        if m:
            out.update(hearing_type=m.group(1), hearing_format=m.group(2).upper(), hearing_date=m.group(3), hearing_time=m.group(4))
        else:
            m = re.search(r'hearing[^\n]{0,60}?\b' + date_re, text, re.I)
            if m: out["hearing_date"] = m.group(1)
    m = re.search(r'JUDGE\s*\n\s*(.+)', text)
    if m: out["judge"] = m.group(1).strip()
    m = re.search(r'COURT ADDRESS\s*\n\s*(.+?)(?:\n\s*\n|$)', text, re.S)
    if m: out["court_address"] = m.group(1).strip().replace("\n", ", ")
    if "This case is pending" in text:
        out["decision"] = "Pending"
    else:
        m = re.search(r'(?:Court Decision|Decision):\s*(.+?)(?:\n|$)', text)
        if m: out["decision"] = m.group(1).strip()
    if "No appeal was received" in text:
        out["appeal"] = "No appeal"
    return out


def _outcome(page):
    if "caseInformation" in page.url:
        page.wait_for_timeout(1500)
        return EoirResult("CASE_FOUND", extract_fields(page.locator("body").inner_text()))
    if page.locator(f'text="{NO_CASE_TEXT}"').count():
        return EoirResult("NO_CASE")
    return None


def _captcha_token(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => [...document.querySelectorAll('textarea[name=\"h-captcha-response\"],"
            " textarea[name=\"g-recaptcha-response\"]')].some(t => t.value && t.value.length > 20)"))
    except Exception:
        return False


def _captcha_showing(page) -> bool:
    return bool(page.locator('iframe[title*="hCaptcha challenge"], div[class*="challenge"]').count()
                or page.locator('text="Please complete the security check"').count())


def check(page, a_number: str, nationality: str = "Burma", on_captcha=None) -> EoirResult:
    digits = re.sub(r"\D", "", a_number)
    if len(digits) == 8:
        digits = "0" + digits
    if len(digits) != 9:
        return EoirResult("ERROR", note="A-Number must have 8 or 9 digits")
    try:
        _to_form(page)
        boxes = page.locator('input[type="number"]')
        if boxes.count() < 9:
            return EoirResult("ERROR", note=f"form has {boxes.count()} digit boxes")
        for i, d in enumerate(digits):
            boxes.nth(i).click(); boxes.nth(i).fill(d)
            page.wait_for_timeout(random.randint(30, 80))
        nat = page.locator('input[type="text"][role="combobox"], input.p-autocomplete-input').first
        if nationality.upper() not in nat.input_value().upper():
            nat.click(); nat.fill(""); page.wait_for_timeout(200)
            nat.fill(nationality); page.wait_for_timeout(random.randint(500, 800))
            opt = page.locator(f'li:has-text("{nationality.upper()}")').first
            try:
                opt.wait_for(state="visible", timeout=4000); opt.click()
            except Exception:
                nat.press("Enter")
            page.wait_for_timeout(300)
        page.locator('button:has-text("Submit")').click()
        page.wait_for_timeout(random.randint(2000, 3000))
        for _ in range(10):
            r = _outcome(page)
            if r:
                return r
            if _captcha_showing(page) or page.locator('iframe[src*="hcaptcha"]').count():
                break
            page.wait_for_timeout(500)
        # Captcha: wait for the person on noVNC
        if on_captcha:
            on_captcha()
        try:
            page.bring_to_front()
        except Exception:
            pass
        end = time.monotonic() + CAPTCHA_TIMEOUT
        last_submit = 0.0
        while time.monotonic() < end:
            page.wait_for_timeout(700)
            r = _outcome(page)
            if r:
                r.captcha = True
                return r
            # Solved = hCaptcha wrote its response token. Then press Submit for the person.
            if _captcha_token(page) and time.monotonic() - last_submit > 6:
                btn = page.locator('button:has-text("Submit")')
                if btn.count():
                    btn.first.click()
                    last_submit = time.monotonic()
                    page.wait_for_timeout(2500)
        return EoirResult("CAPTCHA_TIMEOUT", note="captcha not solved in time", captcha=True)
    except Exception as e:
        return EoirResult("ERROR", note=f"{type(e).__name__}: {str(e)[:150]}")
