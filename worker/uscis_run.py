#!/usr/bin/env python3
"""USCIS case status worker: official API, no browser.

    python -m worker.uscis_run

Looks up each case's receipt once a day (USCIS_RECHECK_HOURS), right away for
new cases and "Check now", and emails the user when the status text changes.
Runs beside the AAF browser worker; it never touches my.uscis.gov.
"""
import json
import logging
import time
from datetime import timedelta

from app import config, db, mailer, security

log = logging.getLogger("uscis")

NOT_FOUND_RECHECK_DAYS = 3


def due_cases(cn, limit=20):
    return cn.execute(
        "SELECT c.*, u.email, u.paid_until FROM cases c JOIN users u ON u.id=c.user_id "
        "WHERE c.uscis_next_at IS NULL OR c.uscis_next_at<=? "
        "ORDER BY c.uscis_next_at IS NOT NULL, c.uscis_next_at, c.id LIMIT ?",
        (db.iso(db.now()), limit)).fetchall()


def notify_change(case, old, new, desc):
    mailer.send(case["email"], f"USCIS update for {case['label']}: {new}",
                f"The USCIS case status for \"{case['label']}\" (receipt ending "
                f"{case['receipt_last4']}) changed.\n\nBefore: {old}\nNow: {new}\n\n{desc}\n\n"
                f"See details: {config.BASE_URL}/app\n\n"
                "From the official USCIS Case Status API. It is not legal advice.\n")


def process(cn, case, client) -> str:
    paid = db.is_paid(case) or case["email"] in config.ADMIN_EMAILS
    if not paid and case["uscis_checked_at"]:
        cn.execute("UPDATE cases SET uscis_next_at=? WHERE id=?", (db.in_(days=1), case["id"]))
        return "skipped"
    receipt = security.decrypt(case["receipt_enc"])
    r = client.case_status(receipt)
    t = db.iso(db.now())
    if r.error:
        cn.execute("UPDATE cases SET uscis_error=?, uscis_next_at=? WHERE id=?",
                   (r.error[:160], db.in_(hours=1), case["id"]))
        return "ERROR"
    if r.ok:
        nxt = db.in_(hours=config.USCIS_RECHECK_HOURS)
    else:
        nxt = db.in_(days=NOT_FOUND_RECHECK_DAYS)
    cn.execute("UPDATE cases SET uscis_status=?, uscis_desc=?, uscis_form=?, uscis_modified=?, "
               "uscis_checked_at=?, uscis_next_at=?, uscis_history_enc=COALESCE(?,uscis_history_enc), uscis_error=NULL WHERE id=?",
               (r.status[:200], r.desc[:2000], r.form[:40], r.modified[:40], t, nxt,
                security.encrypt(json.dumps(r.history)) if r.history is not None else None, case["id"]))
    old = case["uscis_status"]
    if r.ok and old and old != "NOT_FOUND" and old != r.status:
        notify_change(case, old, r.status, r.desc)
    return r.status


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs each request URL, which contains the receipt number. Keep it out of the journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from tracker.uscis_api import NotConfigured, RateLimited, from_config
    cn = db.connect()
    db.init(cn)
    try:
        client = from_config()
    except NotConfigured as e:
        log.warning("%s; USCIS case status is off. Add the keys to .env and restart.", e)
        while True:
            time.sleep(3600)
    while True:
        cases = due_cases(cn)
        if not cases:
            time.sleep(15)
            continue
        for case in cases:
            try:
                status = process(cn, case, client)
            except RateLimited as e:
                log.warning("rate limited (%s); waiting 15 minutes", e)
                time.sleep(900)
                break
            except Exception:
                log.exception("case %s failed", case["id"])
                cn.execute("UPDATE cases SET uscis_error='exception', uscis_next_at=? WHERE id=?",
                           (db.in_(hours=1), case["id"]))
                status = "ERROR"
            log.info("case %s -> %s", case["id"], status)
            if status != "skipped":
                time.sleep(config.USCIS_DELAY)


if __name__ == "__main__":
    main()
