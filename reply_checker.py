#!/usr/bin/env python3
"""
reply_checker.py — cron script (runs every 30 minutes).
Polls each sender's Gmail inbox for replies to outreach threads and classifies
each one (real reply / OOO / left-company) in a single pass, updating the sheet
accordingly.

Usage:
  python3 reply_checker.py          # cron cadence — active rows only
  python3 reply_checker.py --full   # on-demand deep sweep — every row with a
                                     # thread, including resolved ones, in case
                                     # a reply or departure notice was missed
                                     # while the row was still being polled
"""

import fcntl
import logging
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, timedelta

socket.setdefaulttimeout(30)  # prevent any socket call hanging after sleep/wake

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import SENDERS, REPLY_STATUS_RECEIVED, STATUS_FOLLOWUP_INITIATED
from agents.sheet_agent import SheetAgent
from agents.gmail_agent import GmailAgent
from agents.reply_classifier import classify_reply, has_departure_keywords

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR   = os.path.join(_BASE_DIR, "logs")
_LOCK_FILE = os.path.join(_LOG_DIR, "reply_checker.lock")
os.makedirs(_LOG_DIR, exist_ok=True)

_lock_fd = open(_LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    sys.exit(0)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_LOG_DIR, "reply_checker.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("reply_checker")


def _add_working_days(d: date, n: int) -> date:
    """Return d + n working days (Mon–Fri only, no bank-holiday lookup needed here)."""
    current = d
    added = 0
    while added < n:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _parse_gmail_after_date(last_action_date: str) -> str:
    """Convert sheet last_action_date like '10 August 2026 15:30:00' to Gmail after: format 'YYYY/MM/DD'."""
    from datetime import datetime as _dt
    for fmt in ("%d %B %Y %H:%M:%S", "%d %B %Y"):
        try:
            return _dt.strptime(last_action_date.strip(), fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    return ""


def check_sender(sender: dict, include_resolved: bool = False):
    name = sender["email"]
    log.info("[%s] Checking for replies", name)

    try:
        sheet = SheetAgent(sender)
        gmail = GmailAgent(sender)
    except Exception as exc:
        log.error("[%s] Init failed: %s", name, exc)
        return

    rows = sheet.get_rows_for_reply_check(include_resolved=include_resolved)
    if not rows:
        log.info("[%s] No rows to check", name)
        return

    log.info("[%s] Checking %d recipients for replies", name, len(rows))
    for row in rows:
        # Small throttle — each row makes 3-4 Gmail API calls back-to-back, and
        # firing them with zero delay across 80+ rows was blowing through Gmail's
        # per-user rate limit, silently dropping reply detection on the errors.
        time.sleep(0.3)
        recipient = row["recipient_email"]
        thread_id = row.get("thread_id", "")
        try:
            if gmail.has_bounced(recipient):
                log.info("[%s] Bounce detected for %s", name, recipient)
                sheet.mark_bounced(row["row_number"])
                continue

            # Convert last_action_date (e.g. "10 August 2026 15:30:00") to Gmail after: filter
            after_date = _parse_gmail_after_date(row.get("last_action_date", ""))

            # Gather every reply-like signal for this contact before deciding what it
            # means — a genuine reply usually lands in-thread, but an OOO or departure
            # auto-reply frequently arrives as a separate thread (Gmail/Exchange give
            # both the same "Automatic reply" subject shape), so both must be checked.
            bodies = []
            thread_body = ""
            if gmail.has_reply_in_thread(thread_id, recipient):
                thread_body = gmail.get_reply_body_in_thread(thread_id, recipient)
                if thread_body:
                    bodies.append(thread_body)
            _, auto_reply_body = gmail.get_ooo_reply(thread_id, recipient, after_date=after_date)
            if auto_reply_body:
                bodies.append(auto_reply_body)

            if not bodies:
                log.debug("[%s] No reply yet from %s", name, recipient)
                continue

            combined_body = "\n\n---\n\n".join(bodies)
            existing = row.get("next_followup_date")
            existing_comments = row.get("comments", "")
            already_handled_ooo = bool(existing) and isinstance(existing_comments, str) and "OOO" in existing_comments

            # Already tagged OOO and nothing new suggests a departure — skip the LLM
            # call (this is what keeps repeat cycles on a long-running OOO cheap).
            # But a message that actually landed IN the outreach thread is always new,
            # substantive content — a recurring auto-responder only ever resurfaces via
            # get_ooo_reply's separate-thread search, never as a fresh in-thread message —
            # so never suppress classification just because the row is still OOO-tagged
            # when thread_body is present (this is what let a real "thanks, forwarded to
            # HR" reply sit unclassified for over a week after the contact came back from OOO).
            if already_handled_ooo and not thread_body and not has_departure_keywords(combined_body):
                log.debug("[%s] OOO from %s already handled (followup %s), skipping",
                          name, recipient, existing)
                continue

            result = classify_reply(combined_body)
            category = result["category"]

            if category == "real_reply":
                if row.get("reply_status") == REPLY_STATUS_RECEIVED:
                    # Already recorded (only reachable via --full, since the default
                    # scope excludes these) — skip the write so a deep sweep doesn't
                    # churn last_action_date on rows with nothing new to report.
                    log.debug("[%s] Reply from %s already recorded, skipping", name, recipient)
                    continue
                log.info("[%s] Reply detected from %s (thread %s)", name, recipient, thread_id)
                sheet.mark_reply_received(row["row_number"])
                continue

            if category == "left_company":
                log.info("[%s] Reply from %s indicates they're no longer with the company", name, recipient)
                sheet.mark_no_longer_with_company(row["row_number"])
                continue

            # category == "ooo" — only meaningful for a row still actively following
            # up; a resolved row (not interested / discussion in progress, only
            # reachable via --full) has no pending followup to push, so leave it alone.
            if row.get("status") != STATUS_FOLLOWUP_INITIATED:
                log.debug("[%s] OOO from %s on a resolved row, nothing to update", name, recipient)
                continue

            return_date = result["return_date"]
            if return_date:
                today = date.today()
                threshold = _add_working_days(today, 2)
                if return_date <= threshold:
                    log.info("[%s] OOO from %s — return date %s is within 2 working days, ignoring",
                             name, recipient, return_date)
                else:
                    followup_date = _add_working_days(return_date, 2)
                    if existing and existing > followup_date:
                        log.info("[%s] OOO from %s — existing followup %s is later than OOO followup %s, keeping existing",
                                 name, recipient, existing, followup_date)
                    else:
                        log.info("[%s] OOO from %s — return %s → next followup %s",
                                 name, recipient, return_date, followup_date)
                        sheet.mark_ooo(row["row_number"], followup_date, recipient)
            else:
                followup_date = _add_working_days(date.today(), 10)
                if existing and existing > followup_date:
                    log.info("[%s] OOO from %s — no return date found, existing followup %s is later than %s, keeping existing",
                             name, recipient, existing, followup_date)
                else:
                    log.info("[%s] OOO from %s — no return date found, pushing followup to %s",
                             name, recipient, followup_date)
                    sheet.mark_ooo(row["row_number"], followup_date, recipient)
        except Exception as exc:
            log.error("[%s] Error checking replies from %s: %s", name, recipient, exc)


def main():
    full_sweep = "--full" in sys.argv[1:]
    log.info("=== Reply checker started%s ===", " (full sweep)" if full_sweep else "")

    # Each account has its own independent Gmail quota, and the per-row throttle
    # plus retry/backoff (see check_sender) can push a single account's sweep
    # past a minute — run all 3 concurrently (same pattern as email_runner.py's
    # process_sender) so the wall-clock time is the slowest account, not the sum.
    pool = ThreadPoolExecutor(max_workers=len(SENDERS))
    futures = {
        pool.submit(check_sender, sender, full_sweep): sender["email"]
        for sender in SENDERS
    }
    done, pending = wait(futures, timeout=280)

    for future in done:
        sender_email = futures[future]
        try:
            future.result()
        except Exception as exc:
            log.error("Unhandled error for %s: %s", sender_email, exc)

    if pending:
        for future in pending:
            log.error("[%s] Reply check exceeded 280s — abandoning", futures[future])
        pool.shutdown(wait=False)
        os._exit(1)

    pool.shutdown(wait=False)
    log.info("=== Reply checker complete ===")


if __name__ == "__main__":
    main()
