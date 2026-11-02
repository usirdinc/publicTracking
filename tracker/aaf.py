"""AAF (Annual Asylum Fee) lookup on the public USCIS questionnaire.

Ported from usird v2 automation/aaf_check.py (form fill, classify, family
A-Number fallback) without the fleet DB, CSV and operator-profile code.

    with browser_page() as page:
        r = check(page, ["A123456789"], "ZAR1234567890")
        r.status  # DUE | NOT_DUE | EXPIRED | NOT_FOUND | NOT_PENDING | ERROR/... | __BLOCKED__
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from playwright.sync_api import TimeoutError as PWTimeout

URL = "https://my.uscis.gov/accounts/annual-asylum-fee/questionnaire"
A_SEL = 'input[name="questionnaire.alienNumber.number"]'
R_SEL = 'input[name="questionnaire.receiptNumber"]'
CONTINUE = 'button:has-text("Continue to payment")'

# Akamai / block fingerprints — if we see these, STOP immediately.
BLOCK_MARKERS = ("access denied", "reference #", "akamai", "you don't have permission",
                 "request could not be satisfied", "unusual traffic")

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def first_token(cell):
    """CSV cells may hold 'A234; A235' — take the first."""
    return (cell or "").split(";")[0].strip()

def anum_digits(a):
    """'A233623665' -> '233623665' (field auto-pads to 9)."""
    return re.sub(r"\D", "", a or "")

def classify(page):
    """Return (status, amount, note) from the post-submit page."""
    url = page.url.lower()
    body = page.inner_text("body").lower()
    for m in BLOCK_MARKERS:
        if m in body:
            return ("__BLOCKED__", "", m)
    if "pay-and-submit" in url or "your application fee is" in body or "pay for your annual asylum fee" in body:
        raw = page.inner_text("body")
        m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", raw)
        return ("DUE", (m.group(1).replace(",", "") if m else ""), "")
    if "not due for this case" in body:
        return ("NOT_DUE", "", "")
    if "payment period for this case has expired" in body:
        return ("EXPIRED", "", "")
    if "could not find your case" in body or "we could not find" in body:
        return ("NOT_FOUND", "", "")
    if "no longer pending" in body or "you do not need to make a payment" in body:
        return ("NOT_PENDING", "", "")
    # Still sitting on the questionnaire form (A-Number field present) => the
    # submit didn't register (masked-field race). Signal a retry.
    if page.locator(A_SEL).count() > 0:
        return ("__INCOMPLETE__", "", "")
    # Form is gone but no known state matched — a genuinely new response.
    hint = ""
    for line in page.inner_text("body").splitlines():
        s = line.strip()
        if s and any(k in s.lower() for k in ("match", "not found", "invalid", "unable", "error", "cannot", "notice will")):
            hint = s[:160]; break
    return ("ERROR/unclassified", "", hint)

def _digits(s):
    return re.sub(r"\D", "", s or "")

def fill_field(page, sel, value, masked=False, tries=4):
    """Fill a (possibly masked) input and VERIFY the value stuck. The A-Number
    field is a masked control that silently drops fast/partial input, so we
    clear, type char-by-char, and confirm — retrying a few times."""
    loc = page.locator(sel)
    want = _digits(value) if masked else value
    for _ in range(tries):
        loc.click()
        loc.press("ControlOrMeta+A")
        loc.press("Backspace")
        loc.press_sequentially(value, delay=130 if masked else 45)
        page.wait_for_timeout(180)
        got = loc.input_value() or ""
        ok = (want in _digits(got)) if masked else (got.strip().upper() == value.upper())
        if ok:
            return True
    return False

def _attempt(page, a_digits, receipt):
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector(A_SEL, timeout=20000)
    if not fill_field(page, A_SEL, a_digits, masked=True):
        return ("__INCOMPLETE__", "", "anum_fill")
    page.locator(A_SEL).press("Tab")               # commit mask / React state
    if not fill_field(page, R_SEL, receipt, masked=False):
        return ("__INCOMPLETE__", "", "receipt_fill")
    page.locator(R_SEL).press("Tab")
    page.wait_for_timeout(400)
    # Filling the receipt can blur/clear the masked A-Number — re-verify.
    if _digits(a_digits) not in _digits(page.locator(A_SEL).input_value() or ""):
        return ("__INCOMPLETE__", "", "anum_lost")
    page.click(CONTINUE)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1200)
    return classify(page)

def check_one(page, a_digits, receipt, debug_prefix=None):
    """Retry the whole fill+submit up to 3× on the masked-field race."""
    last = ("ERROR/incomplete_form", "", "")
    for _ in range(3):
        st, amt, note = _attempt(page, a_digits, receipt)
        if st != "__INCOMPLETE__":
            return (st, amt, note)
        last = ("ERROR/incomplete_form", "", note)
        page.wait_for_timeout(700)
    if debug_prefix:
        try:
            page.screenshot(path=f"{debug_prefix}.png", full_page=True)
            open(f"{debug_prefix}.txt", "w").write(page.inner_text("body")[:2000])
        except Exception:
            pass
    return last

def check_client(page, a_list, receipt, debug_prefix=None):
    """Asylum AAF is tied to the PRINCIPAL applicant's A-Number. A family shares
    one I-589 receipt but has several A-Numbers, so try each until the case is
    found. Returns (status, amount, note, matched_a). Short-circuits on a real
    state; only reports NOT_FOUND if every A-Number misses."""
    last = ("NOT_FOUND", "", "", a_list[0] if a_list else "")
    for a in a_list:
        st, amt, note = check_one(page, anum_digits(a), receipt, debug_prefix)
        if st not in ("NOT_FOUND",):
            return (st, amt, note, a)
        last = (st, amt, note, a)
    return last


@dataclass
class AafResult:
    status: str
    amount: str
    note: str
    matched_a_number: str
    checked_at: str

    @property
    def blocked(self) -> bool:
        return self.status == "__BLOCKED__"


def check(page, a_numbers, receipt, debug_prefix=None) -> AafResult:
    """Check one case. a_numbers: list of A-Numbers sharing the I-589 receipt."""
    a_list = [a.strip() for a in a_numbers if a and a.strip()]
    receipt = first_token(receipt)
    if not a_list or not receipt:
        return AafResult("ERROR/missing_input", "", "", "", now_iso())
    st, amt, note, matched = check_client(page, a_list, receipt, debug_prefix)
    return AafResult(st, amt, note, matched, now_iso())
