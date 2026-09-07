#!/bin/bash
# setup_cron.sh — installs cron jobs for the AM_UK email system.
# Run once after completing OAuth setup:   bash setup_cron.sh
#
# Cron runs in IST (India Standard Time = UTC+5:30), the system's local timezone.
# email_runner.py checks UK local time internally and self-exits if outside window,
# so cron just needs to bracket the active BST windows in IST equivalents.
#
# Send windows (UK BST) → IST equivalents (config/settings.py is the source of truth):
#   Mon–Thu   10:00–16:00 BST  =  14:30–20:30 IST
#   Friday    08:30–12:30 BST  =  13:00–17:00 IST
#
# Runner fires every 15 min all day Mon–Fri; email_runner.py self-exits on any slot
# outside its own window, so the cron entry doesn't need day-specific ranges.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "${LOG_DIR}"

echo "AM_UK Email System — Cron Setup"
echo "Project: ${PROJECT_DIR}"
echo "Python:  ${PYTHON}"
echo ""

# ── Cron entries ───────────────────────────────────────────────────────────────

# Fire every 15 min all day Mon–Fri — email_runner.py self-exits if outside BST windows
EMAIL_JOB="*/15 * * * 1-5 cd \"${PROJECT_DIR}\" && ${PYTHON} email_runner.py >> ${LOG_DIR}/email_runner.log 2>&1"

# ── Install ────────────────────────────────────────────────────────────────────

# Only ever remove lines carrying THIS project's marker — a blanket grep on
# script names like "email_runner" would also strip other projects' cron jobs
# that happen to run a same-named script from a different directory.
MARKER="# AM_UK_EMAIL_SYSTEM"
(
  crontab -l 2>/dev/null | grep -v "${MARKER}"
  echo ""
  echo "${MARKER}"
  echo "${EMAIL_JOB} ${MARKER}"
) | crontab -

echo "Cron jobs installed:"
echo ""
crontab -l | grep -A2 "${MARKER}"
echo ""
echo "Schedule (UK local time) — email_runner handles sort, reply check, and log after each run:"
echo "  Mon–Thu  10:00–16:00  every 15 min"
echo "  Friday   08:30–12:30  every 15 min"
echo "  Cron fires all day Mon–Fri — script self-exits outside active windows"
echo ""
echo "View logs:"
echo "  tail -f ${LOG_DIR}/sheet_sort.log"
echo "  tail -f ${LOG_DIR}/email_runner.log"
echo "  tail -f ${LOG_DIR}/reply_checker.log"
echo ""
echo "To remove cron jobs:"
echo "  crontab -e    (delete lines containing AM_UK_EMAIL_SYSTEM)"
