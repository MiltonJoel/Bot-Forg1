#!/usr/bin/env bash
# Always run with the project venv; the system python has a broken loguru.
cd "$(dirname "$0")"

# ponytail: refuse to start headful with no display instead of letting Chrome
# die-and-relaunch every 15s forever (25 launches in 9 min, load 12, on
# 2026-08-18). systemd's RestartSec retry is free; a Chrome launch is not.
if [ "${HEADLESS:-$(grep -oP '^HEADLESS=\K.*' .env 2>/dev/null)}" = "false" ] && [ -z "$DISPLAY" ]; then
  echo "No DISPLAY and HEADLESS=false — refusing to launch headful Chrome." >&2
  exit 1
fi

exec .venv/bin/python main.py "$@"
