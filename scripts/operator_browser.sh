#!/usr/bin/env bash
# Open the operator browser on the VPS screen (:99) with its own saved profile.
# Sign in to Google once here (through noVNC); the login is kept in the profile
# for the EOIR operator queue. The AAF worker never uses this profile.
#
#   bash scripts/operator_browser.sh            # opens Google sign-in
#   bash scripts/operator_browser.sh <url>      # opens another page
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="${OPERATOR_PROFILE:-$PWD/data/operator-profile}"
mkdir -p "$PROFILE"; chmod 700 "$PROFILE"
CHROME="$(command -v google-chrome || command -v google-chrome-stable || true)"
if [ -z "$CHROME" ]; then
  echo "Google Chrome is not installed. Run once:"
  echo "  sudo $PWD/.venv/bin/python -m playwright install --with-deps chrome"
  exit 1
fi
# Real Chrome, not Playwright's test build, so Google allows the sign-in.
DISPLAY=:99 nohup "$CHROME" --user-data-dir="$PROFILE" --no-first-run \
  --no-default-browser-check --disable-dev-shm-usage --start-maximized \
  "${1:-https://accounts.google.com/}" >/dev/null 2>&1 &
echo "Chrome is open on the VPS screen. View it with noVNC: http://localhost:6080/vnc.html"
