#!/bin/bash
# smoke.sh — Drive the AM_UK pipeline for a quick sanity check.
# Run from the project root: bash .claude/skills/run-am-uk/smoke.sh [--full]
#
# Default (fast, ~10s):  window check + sheet sort.
# --full (~2min):        also runs reply_checker.py against live Gmail.
#
# All scripts write to logs/. Nothing is sent or modified without --send.

set -e
cd "$(dirname "$0")/../../.."   # project root
PYTHON=/Users/mac/Documents/AM_UK/.venv/bin/python3

echo "=== AM_UK pipeline smoke ==="
echo ""

# ── 1. Window check ───────────────────────────────────────────────────────────
echo "-- email_runner.py (window check) --"
$PYTHON email_runner.py 2>&1 | grep -E 'INFO.*email_runner|ERROR|Traceback' | tail -3
echo ""

# ── 2. Sheet sort ─────────────────────────────────────────────────────────────
echo "-- sheet_sort.py --"
$PYTHON sheet_sort.py 2>&1 | grep -v FutureWarning | grep -v warnings.warn | grep -v '^\s*$' | head -20
echo ""

# ── 3. Reply checker (optional) ───────────────────────────────────────────────
if [[ "$1" == "--full" ]]; then
  echo "-- reply_checker.py (live Gmail check) --"
  $PYTHON reply_checker.py 2>&1 | grep -E 'INFO.*reply_checker|WARNING|ERROR|Traceback' | tail -20
  echo ""
fi

echo "=== Done ==="
