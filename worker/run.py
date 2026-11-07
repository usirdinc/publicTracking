#!/usr/bin/env python3
"""AAF check worker: one browser, one job at a time, safe pace.

    DISPLAY=:99 python -m worker.run

Stops all checks for 6 hours on the first block page, and for 1 hour after
6 errors in a row, and emails the admins. systemd restarts it if it crashes.
"""
import logging
import random
import time
from datetime import timedelta

from app import config, db, mailer, notifications, security

log = logging.getLogger("worker")

MAX_ATTEMPTS = 3
RESTART_BROWSER_EVERY = 50
IDLE_CLOSE_SECONDS = 30


def claim(cn):
    with db.tx(cn):
        j = cn.execute("SELECT * FROM jobs WHERE state='queued' AND run_after<=? "
                       "ORDER BY priority, run_after, id LIMIT 1", (db.iso(db.now()),)).fetchone()
        if not j:
            return None
        cn.execute("UPDATE jobs SET state='running', started_at=?, attempts=attempts+1, "
                   "progress=10, stage='Starting secure browser' WHERE id=?",
                   (db.iso(db.now()), j["id"]))
    return cn.execute("SELECT * FROM jobs WHERE id=?", (j["id"],)).fetchone()


def pause(cn, hours: float, reason: str):
    until = db.now() + timedelta(hours=hours)
    db.set_setting(cn, "paused_until", db.iso(until))
    db.set_setting(cn, "pause_reason", reason)
    log.error("PAUSED until %s: %s", until, reason)
    for a in config.ADMIN_EMAILS:
        mailer.send(a, "AAF tracker: checks paused",
                    f"All checks are paused until {until} UTC.\nReason: {reason}\n"
                    f"Resume early from {config.BASE_URL}/admin once you have looked into it.")


def notify_change(user_email, case, old, new, amount):
    from app.main import status_text  # same wording as the site
    text, _ = status_text(new, amount)
    notifications.send_update(case, f"AAF update for {case['label']}: {text}",
                f"Your Annual Asylum Fee status for \"{case['label']}\" "
                f"(A-Number ending {case['a_last3']}) changed.\n\nNow: {text}\n\n"
                f"See details: {config.BASE_URL}/app\n\n"
                "This is an automated check of the public USCIS page. It is not legal advice.\n"
                "To pay, use the official USCIS site: https://my.uscis.gov/accounts/annual-asylum-fee/questionnaire\n", "aaf")


def process(cn, job, checker) -> str:
    """Run one job. checker(a_numbers:list, receipt:str) -> (status, amount, note, matched).
    Returns the status (or 'skipped')."""
    case = cn.execute("SELECT * FROM cases WHERE id=?", (job["case_id"],)).fetchone()
    if not case:
        cn.execute("UPDATE jobs SET state='done', finished_at=?, result_status='gone' WHERE id=?",
                   (db.iso(db.now()), job["id"]))
        return "skipped"
    user = cn.execute("SELECT * FROM users WHERE id=?", (case["user_id"],)).fetchone()
    if job["kind"] == "scheduled" and not db.is_paid(user) and user["email"] not in config.ADMIN_EMAILS:
        cn.execute("UPDATE jobs SET state='done', finished_at=?, result_status='unpaid' WHERE id=?",
                   (db.iso(db.now()), job["id"]))
        return "skipped"

    anums = security.decrypt(case["a_numbers_enc"]).split(";")
    receipt = security.decrypt(case["receipt_enc"])
    try:
        if hasattr(checker, "current"):
            checker.current["job"] = job["id"]   # so progress updates land on this job
        status, amount, note, _matched = checker(anums, receipt)
    except Exception as e:  # browser crash etc.
        status, amount, note = "ERROR/exception", "", str(e)[:160]

    t = db.iso(db.now())
    if status == "__BLOCKED__":
        cn.execute("UPDATE jobs SET state='queued', run_after=?, attempts=attempts-1 WHERE id=?",
                   (db.in_(hours=6), job["id"]))
        pause(cn, 6, f"USCIS block page seen ({note})")
        return status

    if status.startswith("ERROR"):
        if job["attempts"] < MAX_ATTEMPTS:
            cn.execute("UPDATE jobs SET state='queued', run_after=?, result_status=? WHERE id=?",
                       (db.in_(minutes=20 * job["attempts"]), status, job["id"]))
        else:
            with db.tx(cn):
                cn.execute("UPDATE jobs SET state='failed', finished_at=?, result_status=? WHERE id=?",
                           (t, status, job["id"]))
                cn.execute("UPDATE cases SET status=?, note=?, checked_at=? WHERE id=? AND status IS NULL",
                           (status, note[:160], t, case["id"]))
                _schedule_next(cn, case["id"], db.in_(days=1))
        return status

    old = case["status"]
    with db.tx(cn):
        cn.execute("INSERT INTO checks(case_id,status,amount,note,checked_at) VALUES(?,?,?,?,?)",
                   (case["id"], status, amount, note[:160], t))
        nxt = db.in_(hours=config.AAF_RECHECK_HOURS)
        cn.execute("UPDATE cases SET status=?, amount=?, note=?, checked_at=?, next_check_at=? WHERE id=?",
                   (status, amount, note[:160], t, nxt, case["id"]))
        cn.execute("UPDATE jobs SET state='done', finished_at=?, result_status=?, progress=100, stage='Done' "
                   "WHERE id=?", (t, status, job["id"]))
        _schedule_next(cn, case["id"], nxt)
    known_old = old if old and not old.startswith("ERROR") else None
    if (known_old and known_old != status) or (known_old is None and status == "DUE"):
        notify_change(user["email"], case, known_old, status, amount)
    return status


def _schedule_next(cn, case_id, when):
    cn.execute("DELETE FROM jobs WHERE case_id=? AND kind='scheduled' AND state='queued'", (case_id,))
    db.enqueue(cn, case_id, "scheduled", when)


def has_work(cn) -> bool:
    return cn.execute("SELECT 1 FROM jobs WHERE state='queued' AND run_after<=? LIMIT 1",
                      (db.iso(db.now()),)).fetchone() is not None


def paused(cn) -> bool:
    pu = db.parse(db.get_setting(cn, "paused_until"))
    return bool(pu and pu > db.now())


def make_checker(page, cn=None):
    from tracker import aaf
    from tracker.aaf import check
    current = {"job": None}

    def hook(pct, stage):
        if cn is not None and current["job"]:
            cn.execute("UPDATE jobs SET progress=?, stage=? WHERE id=?", (pct, stage, current["job"]))
    aaf.progress_hook = hook

    def _check(anums, receipt):
        r = check(page, anums, receipt)
        return r.status, r.amount, r.note, r.matched_a_number
    _check.current = current
    return _check


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from tracker.browser import browser_page
    cn = db.connect()
    db.init(cn)
    cn.execute("UPDATE jobs SET state='queued', attempts=MAX(attempts-1,0) WHERE state='running'")
    consecutive_err = 0
    while True:
        # Keep the screen clear: open the browser only when there is work.
        if paused(cn) or not has_work(cn):
            time.sleep(5)
            continue
        done = 0
        idle_since = time.monotonic()
        with browser_page(headful=config.get("HEADLESS", "0") != "1") as page:
            checker = make_checker(page, cn)
            while done < RESTART_BROWSER_EVERY:
                if paused(cn):
                    break
                job = claim(cn)
                if not job:
                    if time.monotonic() - idle_since > IDLE_CLOSE_SECONDS:
                        break  # queue empty for a while: close the browser
                    time.sleep(2)
                    continue
                status = process(cn, job, checker)
                log.info("job %s case %s (%s) -> %s", job["id"], job["case_id"], job["kind"], status)
                if status == "skipped":
                    continue
                done += 1
                consecutive_err = consecutive_err + 1 if status.startswith("ERROR") else 0
                if consecutive_err >= 6:
                    pause(cn, 1, "6 errors in a row (form changed or blocked?)")
                    consecutive_err = 0
                if status == "__BLOCKED__":
                    break
                if status == "ERROR/exception":
                    log.warning("browser error; relaunching the browser")
                    break  # page/browser likely dead (e.g. screen restarted): start fresh
                time.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
                idle_since = time.monotonic()


if __name__ == "__main__":
    main()
