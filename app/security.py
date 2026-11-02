"""Password hashing, encryption of A-Numbers/receipts at rest, CSRF tokens."""
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from cryptography.fernet import Fernet

from app import config

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _fernet() -> Fernet:
    if not config.DATA_KEY:
        raise RuntimeError("DATA_KEY is not set")
    return Fernet(config.DATA_KEY.encode())


def encrypt(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode()


def csrf_token(session: dict) -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        session["csrf"] = tok
    return tok


def csrf_ok(session: dict, sent: str | None) -> bool:
    tok = session.get("csrf")
    return bool(tok and sent and hmac.compare_digest(tok, sent))


A_RE = re.compile(r"^A?\d{8,9}$")
RECEIPT_RE = re.compile(r"^[A-Z]{3}\d{10}$")


def normalize_a_numbers(raw: str) -> list[str]:
    """'A123-456-789; 987654321' -> ['A123456789', 'A987654321']; raises ValueError."""
    out = []
    for part in re.split(r"[;,\s]+", raw or ""):
        d = re.sub(r"\D", "", part)
        if not d:
            continue
        if len(d) not in (8, 9):
            raise ValueError("Each A-Number must have 8 or 9 digits.")
        out.append("A" + d.zfill(9))
    if not out:
        raise ValueError("Enter at least one A-Number.")
    if len(out) > 6:
        raise ValueError("At most 6 A-Numbers per case.")
    return out


def normalize_receipt(raw: str) -> str:
    r = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not RECEIPT_RE.match(r):
        raise ValueError("Receipt numbers look like ABC1234567890 (3 letters, 10 digits).")
    return r
