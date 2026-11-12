#!/bin/sh
set -eu
cd "$HOME/case-tracker-operator"
exec .venv/bin/python -m scripts.staff_shift_runner --workers 4 --service both
