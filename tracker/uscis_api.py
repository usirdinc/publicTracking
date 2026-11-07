"""Client for the official USCIS Case Status API (developer.uscis.gov).

Same flow as USIRD v2's api/services/USCISService.php:
  token:  POST {token_url}  grant_type=client_credentials, header demo_id
  lookup: GET  {base_url}{receipt}   Authorization: Bearer <token>
On a 401 the token is refreshed and the lookup retried once.
No browser and no scraping, so it carries no Akamai block risk.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class RateLimited(Exception):
    """USCIS answered 429. Back off before the next call."""


class NotConfigured(Exception):
    pass


@dataclass
class CaseStatus:
    ok: bool                 # True when USCIS returned a case
    status: str = ""         # e.g. "Case Was Received" ; "NOT_FOUND" when no case
    desc: str = ""
    form: str = ""
    submitted: str = ""
    modified: str = ""
    http_code: int = 0
    error: str = ""          # set when the lookup failed (try again later)
    history: list[dict] | None = None


class UscisClient:
    def __init__(self, client_id, client_secret, demo_id="",
                 token_url="https://api.uscis.gov/oauth/accesstoken",
                 base_url="https://api.uscis.gov/case-status/", http: httpx.Client | None = None):
        if not (client_id and client_secret):
            raise NotConfigured("USCIS_CLIENT_ID / USCIS_CLIENT_SECRET not set")
        self.client_id, self.client_secret, self.demo_id = client_id, client_secret, demo_id
        self.token_url = token_url
        self.base_url = base_url.rstrip("/") + "/"
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_exp = 0.0

    def _fetch_token(self) -> str:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.demo_id:
            headers["demo_id"] = self.demo_id
        r = self.http.post(self.token_url, headers=headers, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id, "client_secret": self.client_secret})
        if r.status_code == 429:
            raise RateLimited("token endpoint 429")
        r.raise_for_status()
        d = r.json()
        self._token = d["access_token"]
        # refresh a minute early; USCIS tokens last about 30 minutes
        self._token_exp = time.time() + max(int(d.get("expires_in", 1799)) - 60, 60)
        return self._token

    def token(self) -> str:
        if not self._token or time.time() >= self._token_exp:
            return self._fetch_token()
        return self._token

    def case_status(self, receipt: str) -> CaseStatus:
        receipt = receipt.strip().upper()
        for attempt in (1, 2):
            try:
                r = self.http.get(self.base_url + receipt,
                                  headers={"Authorization": f"Bearer {self.token()}",
                                           "Accept": "application/json"})
            except httpx.HTTPError as e:
                return CaseStatus(False, error=f"network: {type(e).__name__}")
            if r.status_code == 401 and attempt == 1:
                self._token = None          # expired or revoked: refresh once
                continue
            break
        code = r.status_code
        if code == 429:
            raise RateLimited("case-status 429")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if code == 200 and isinstance(body.get("case_status"), dict):
            cs = body["case_status"]
            return CaseStatus(
                True,
                status=(cs.get("current_case_status_text_en") or "").strip(),
                desc=(cs.get("current_case_status_desc_en") or "").strip(),
                form=(cs.get("formType") or "").strip(),
                submitted=(cs.get("submittedDate") or "").strip(),
                modified=(cs.get("modifiedDate") or "").strip(),
                http_code=code,
                history=normalize_history(cs.get("hist_case_status")))
        if code in (400, 404, 422):
            # USCIS has no case for this receipt (or the format is rejected)
            msg = body.get("message") or body.get("error") or ""
            return CaseStatus(False, status="NOT_FOUND", desc=str(msg)[:200], http_code=code)
        return CaseStatus(False, http_code=code, error=f"HTTP {code}")


def from_config() -> UscisClient:
    from app import config
    return UscisClient(config.USCIS_CLIENT_ID, config.USCIS_CLIENT_SECRET, config.USCIS_DEMO_ID,
                       config.USCIS_TOKEN_URL, config.USCIS_API_BASE_URL)


def normalize_history(value):
    if not isinstance(value, list):
        return None
    result = []
    seen = set()
    for item in value[:200]:
        if not isinstance(item, dict):
            continue
        text = item.get('completed_text_en')
        date = item.get('date')
        if not isinstance(text, str) or not text.strip():
            continue
        entry = {'date': date[:80] if isinstance(date,str) else '', 'text': text.strip()[:4000]}
        key = (entry['date'],entry['text'])
        if key not in seen:
            result.append(entry)
            seen.add(key)
    return result
