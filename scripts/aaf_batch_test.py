#!/usr/bin/env python3
"""Run AAF checks from a CSV and score them against known answers.

CSV columns: client_id, a_numbers (';'-separated), i589_asylum_receipt, expected
    python scripts/aaf_batch_test.py --in aaf_test_20.csv --out results/aaf_test_20_results.csv

Safe pace by default (3-8 s between cases, one browser). Stops on the first
block page and after 6 errors in a row. Prints only client ids, never A-Numbers.
"""
import argparse, csv, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker.aaf import check
from tracker.browser import browser_page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", required=True)
    ap.add_argument("--min-delay", type=float, default=3.0)
    ap.add_argument("--max-delay", type=float, default=8.0)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.infile, newline="", encoding="utf-8-sig")))
    if a.limit:
        rows = rows[:a.limit]
    os.makedirs(os.path.dirname(a.outfile) or ".", exist_ok=True)
    dbg_dir = os.path.join(os.path.dirname(a.outfile) or ".", "debug")
    os.makedirs(dbg_dir, exist_ok=True)

    matched = mismatched = errors = 0
    consecutive_err = 0
    started = time.time()
    with open(a.outfile, "w", newline="") as out, browser_page(headful=not a.headless) as page:
        w = csv.writer(out)
        w.writerow(["client_id", "expected", "status", "amount", "note", "match", "seconds", "checked_at"])
        for i, r in enumerate(rows, 1):
            cid = str(r.get("client_id"))
            exp = (r.get("expected") or "").strip().upper()
            t0 = time.time()
            try:
                res = check(page, (r.get("a_numbers") or "").split(";"), r.get("i589_asylum_receipt"),
                            debug_prefix=os.path.join(dbg_dir, f"aaf_{cid}"))
                st, amt, note, at = res.status, res.amount, res.note, res.checked_at
            except Exception as e:
                st, amt, note, at = "ERROR/exception", "", str(e)[:160], ""
            secs = round(time.time() - t0, 1)
            ok = (st == exp) if exp else ""
            w.writerow([cid, exp, st, amt, note, ok, secs, at]); out.flush()
            print(f"[{i:>2}/{len(rows)}] client {cid:>6}: {st:<20} expected {exp:<10} {'OK' if ok is True else ('MISMATCH' if ok is False else '')} {secs}s", flush=True)

            if st == "__BLOCKED__":
                print(f"\nBLOCK DETECTED ({note!r}). Stopping now. Do not retry from this IP today.")
                break
            if st.startswith("ERROR"):
                errors += 1; consecutive_err += 1
            else:
                consecutive_err = 0
            if ok is True: matched += 1
            elif ok is False: mismatched += 1
            if consecutive_err >= 6:
                print("\n6 errors in a row. Stopping (likely blocked or the form changed).")
                break
            if i < len(rows):
                time.sleep(random.uniform(a.min_delay, a.max_delay))

    print(f"\nSummary: {matched} matched, {mismatched} mismatched, {errors} errors, "
          f"{round(time.time()-started)}s total. Results: {a.outfile}")


if __name__ == "__main__":
    main()
