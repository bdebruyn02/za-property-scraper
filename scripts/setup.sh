#!/usr/bin/env sh
# One-time setup on macOS/Linux.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
echo "Setup done. Start the web UI with scripts/start_ui.sh"
