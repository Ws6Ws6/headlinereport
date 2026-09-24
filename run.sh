#!/usr/bin/env bash
# Regenerate public/index.html from live RSS feeds.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python build.py "$@"
