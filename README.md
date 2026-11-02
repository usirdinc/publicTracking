# publicTracking

AAF (Annual Asylum Fee) and EOIR status tracking for the USIRD subscription app.
Step 1: prove AAF checks work from the worker VPS without being blocked.

## Run the 20-case test on the VPS

    cd ~/tracker/publicTracking
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt && python -m playwright install chromium
    python -m pytest -q            # parser tests, no network
    python scripts/aaf_batch_test.py --in ~/aaf_test_20.csv --out results/aaf_test_20_results.csv

Watch it live in noVNC (`ssh -L 6080:127.0.0.1:6080 tracker@<vps-ip>`, then
http://localhost:6080/vnc.html). Test CSVs and results are git-ignored: they hold A-Numbers.
