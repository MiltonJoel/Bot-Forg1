#!/usr/bin/env bash
# Always run with the project venv; the system python has a broken loguru.
cd "$(dirname "$0")"
exec .venv/bin/python main.py "$@"
