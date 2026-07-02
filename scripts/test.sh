#!/usr/bin/env bash
# v30 §WS-G — local test runner that mirrors CI exactly.
#
# Usage:
#   ./scripts/test.sh                 # run the whole suite
#   ./scripts/test.sh -k snapshot     # filter (passes through to pytest)
#   ./scripts/test.sh --tb=short      # any pytest flag works
#
# The script picks the venv interpreter from jp-form-outreach/.venv when
# present (matches the README setup), falling back to the system python3.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PY="$ROOT_DIR/jp-form-outreach/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

cd "$ROOT_DIR"

# Ensure pytest is installed — silently if the venv already has it. New
# checkouts get a one-time install.
if ! "$PY" -c "import pytest" >/dev/null 2>&1; then
  "$PY" -m pip install --quiet pytest pyyaml
fi

# v32 FX5 — syntax gate BEFORE pytest. The watchdog LaunchAgent imports
# _outreach_core with the production interpreter; a stray tab/3.10-only
# syntax once killed it silently for a month (data/watchdog.err TabError).
# compileall catches that class in seconds, on the same interpreter pytest
# uses.
"$PY" -m compileall -q _outreach_core jp-form-outreach/run.py linkedin-outreach/run.py

exec "$PY" -m pytest _outreach_core/tests "$@"
