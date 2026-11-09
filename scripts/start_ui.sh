#!/usr/bin/env sh
cd "$(dirname "$0")/.."
exec .venv/bin/python web_ui.py "$@"
