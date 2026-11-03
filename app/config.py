"""Settings from environment (.env on the VPS, never committed)."""
import os
import re
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s+#", v, maxsplit=1)[0]   # drop inline " # comment"
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


ROOT = Path(__file__).resolve().parents[1]
_load_dotenv(ROOT / ".env")


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DB_PATH = get("DB_PATH", str(ROOT / "data" / "tracker.sqlite3"))
SECRET_KEY = get("SECRET_KEY")              # session signing, 32+ random chars
DATA_KEY = get("DATA_KEY")                  # Fernet key for A-Numbers/receipts
BASE_URL = get("BASE_URL", "http://localhost:8000").rstrip("/")
BETA_CAP = int(get("BETA_CAP", "200"))
ADMIN_EMAILS = {e.strip().lower() for e in get("ADMIN_EMAILS").split(",") if e.strip()}

SQUARE_CHECKOUT_URL = get("SQUARE_CHECKOUT_URL")          # Square payment link for the monthly plan
PRICE = get("PRICE", "$10")                                # shown on the site; must match the Square plan
SQUARE_ACCESS_TOKEN = get("SQUARE_ACCESS_TOKEN")
SQUARE_WEBHOOK_SIGNATURE_KEY = get("SQUARE_WEBHOOK_SIGNATURE_KEY")
SQUARE_ENV = get("SQUARE_ENV", "production")               # or sandbox
PAID_DAYS_PER_PAYMENT = int(get("PAID_DAYS_PER_PAYMENT", "35"))

SMTP_HOST = get("SMTP_HOST")
SMTP_PORT = int(get("SMTP_PORT", "587"))
SMTP_USER = get("SMTP_USER")
SMTP_PASS = get("SMTP_PASS")
MAIL_FROM = get("MAIL_FROM", SMTP_USER)

# Check pacing (one browser, one IP)
MIN_DELAY = float(get("CHECK_MIN_DELAY", "3"))
MAX_DELAY = float(get("CHECK_MAX_DELAY", "8"))
NEW_CASES_PER_DAY = int(get("NEW_CASES_PER_DAY", "3"))
INSTANT_CHECKS_PER_CASE_PER_DAY = int(get("INSTANT_CHECKS_PER_CASE_PER_DAY", "1"))
MAX_CASES_PER_USER = int(get("MAX_CASES_PER_USER", "5"))

# Official USCIS Case Status API (same credentials as USIRD v2 admin > USCIS)
USCIS_CLIENT_ID = get("USCIS_CLIENT_ID")
USCIS_CLIENT_SECRET = get("USCIS_CLIENT_SECRET")
USCIS_DEMO_ID = get("USCIS_DEMO_ID")
USCIS_TOKEN_URL = get("USCIS_TOKEN_URL", "https://api.uscis.gov/oauth/accesstoken")
USCIS_API_BASE_URL = get("USCIS_API_BASE_URL", "https://api.uscis.gov/case-status/")
USCIS_DELAY = float(get("USCIS_DELAY", "2"))               # seconds between lookups
USCIS_RECHECK_HOURS = float(get("USCIS_RECHECK_HOURS", "24"))
