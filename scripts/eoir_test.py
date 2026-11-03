#!/usr/bin/env python3
"""EOIR test run from the VPS with the signed-in operator Chrome profile.

    DISPLAY=:99 .venv/bin/python scripts/eoir_test.py --in aaf_test_20.csv --limit 5

CSV needs client_id and a_numbers (';'-separated; the first is used), optional
nationality (default Burma). Watch http://localhost:6080/vnc.html through the SSH
tunnel and solve any captcha by hand. Prints client ids only, never A-Numbers.
"""
import argparse, csv, json, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker.eoir import check, operator_page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", default="results/eoir_test.csv")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--min-delay", type=float, default=8)
    ap.add_argument("--max-delay", type=float, default=20)
    ap.add_argument("--nationality", default="Burma")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.infile, newline="", encoding="utf-8-sig")))[: a.limit or None]
    os.makedirs(os.path.dirname(a.outfile) or ".", exist_ok=True)
    counts, captchas, t0 = {}, 0, time.time()
    with open(a.outfile, "w", newline="") as out, operator_page() as page:
        w = csv.writer(out)
        w.writerow(["client_id", "status", "captcha", "seconds", "fields", "note"])
        for i, r in enumerate(rows, 1):
            cid = r.get("client_id")
            anum = (r.get("a_numbers") or r.get("a_number") or "").split(";")[0]
            nat = r.get("nationality") or a.nationality
            print(f"[{i}/{len(rows)}] client {cid} ... ", end="", flush=True)
            s = time.time()
            res = check(page, anum, nat, on_captcha=lambda: print("CAPTCHA: solve it on noVNC ... ", end="", flush=True))
            secs = round(time.time() - s, 1)
            counts[res.status] = counts.get(res.status, 0) + 1
            captchas += res.captcha
            extra = res.fields.get("hearing_date") or res.fields.get("hearing") or res.fields.get("decision") or ""
            print(f"{res.status} {extra} ({secs}s){' ' + res.note if res.note else ''}")
            w.writerow([cid, res.status, int(res.captcha), secs, json.dumps(res.fields), res.note]); out.flush()
            if i < len(rows):
                time.sleep(random.uniform(a.min_delay, a.max_delay))
    print(f"\nSummary: {counts}, captchas solved/asked: {captchas}, {round(time.time()-t0)}s total. Results: {a.outfile}")


if __name__ == "__main__":
    main()
