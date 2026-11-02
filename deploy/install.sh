#!/usr/bin/env bash
# Run on the VPS as user tracker, from ~/publicTracking, after `git pull`.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "Create .env from .env.example first"; exit 1; }
chmod 600 .env
[[ -d .venv ]] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python -m playwright install chromium >/dev/null
python -m pytest -q
sudo cp deploy/tracker-web.service deploy/tracker-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tracker-web tracker-worker
sudo systemctl restart tracker-web tracker-worker
sleep 2
curl -fsS http://127.0.0.1:8000/healthz && echo
echo "Web + worker running. Logs: journalctl -u tracker-web -u tracker-worker -f"
