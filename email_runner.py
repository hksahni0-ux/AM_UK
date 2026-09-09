#!/usr/bin/env python3
"""
email_runner.py — main cron script.

Schedule (UK local time — shared across UK and Ireland rows):
  Mon–Thu  10:00–16:00  followups first, fresh fallback  (25 per sender)
  Friday   08:30–12:30  followups first, fresh fallback  (25 per sender)

Skips each row's own country's bank holidays automatically (UK rows use the
England & Wales gov.uk calendar, Ireland rows use the Nager.Date IE calendar) —
a holiday in one country doesn't halt sends to the other.
"""

import fcntl
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

socket.setdefaulttimeout(30)  # prevent any socket call hanging after sleep/wake

import pytz
import requests
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    SENDERS, TIMEZONE,
    DAILY_LIMIT_WEEKDAY, DAILY_LIMIT_FRIDAY, DAILY_PER_TIER,
    MORNING_WINDOW, FRIDAY_WINDOW,
    FOLLOWUP_GAP_WORKING_DAYS,
    PAUSE_UK_FRESH_SENDS,
)
from agents.sheet_agent import SheetAgent
from agents.research_agent import find_matching_role
from agents.email_writer import write_email
from agents.gmail_agent import GmailAgent
from agents.cv_agent import tailor_cv
from sheet_sort import main as sort_all_sheets

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR  = os.path.join(_BASE_DIR, "logs")
_LOCK_FILE = os.path.join(_LOG_DIR, "email_runner.lock")
os.makedirs(_LOG_DIR, exist_ok=True)

# Exit immediately if another instance is still running
_lock_fd = open(_LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    sys.exit(0)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_LOG_DIR, "email_runner.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("email_runner")

_BANK_HOLIDAY_CACHE = {
    "UK":      os.path.join(os.path.dirname(__file__), "config", "uk_bank_holidays.json"),
    "Ireland": os.path.join(os.path.dirname(__file__), "config", "ie_bank_holidays.json"),
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Malformed-recipient recovery ────────────────────────────────────────────

def _is_invalid_recipient_error(exc: Exception) -> bool:
    """True only for Gmail's permanent 'this address is malformed' rejection —
    never for transient errors (network, quota, auth), which should keep
    retrying/probing rather than getting the row marked bounced."""
    if not isinstance(exc, HttpError) or exc.resp.status != 400:
        return False
    msg = str(exc).lower()
    return "invalid to header" in msg or "invalidargument" in msg


def _repair_invalid_recipient(recipient: str, company_website: str, first_name: str = "") -> str:
    """Best-effort fix for a malformed address, anchored to the row's own
    company domain so a repair can only ever land on the same company —
    never guesses a different person or domain. Handles '@' being dropped
    entirely, and a single stray character standing in for '@' (e.g. a
    keyboard/paste glitch turning 'name@domain.com' into 'name2domain.com').
    Prefers the candidate consistent with the row's known first name, then
    falls back to trying the non-mutating fix (plain insert) before the
    mutating one (drop the trailing stray char). Returns "" if no safe
    repair is found."""
    if not recipient or "@" in recipient or not company_website:
        return ""
    netloc = urlparse(company_website if "://" in company_website else f"//{company_website}").netloc
    domain = netloc.split(":")[0].lower()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain:
        return ""
    idx = recipient.lower().rfind(domain)
    if idx <= 0:
        return ""
    local, rest = recipient[:idx], recipient[idx:]

    candidates = []
    fn = first_name.strip().lower()
    if fn and local.lower().startswith(fn) and len(local) == len(fn) + 1:
        candidates.append(f"{local[:len(fn)]}@{rest}")
    candidates.append(f"{local}@{rest}")
    candidates.append(f"{local[:-1]}@{rest}")

    for candidate in candidates:
        if _EMAIL_RE.match(candidate):
            return candidate
    return ""


# ── Bank holidays ─────────────────────────────────────────────────────────────

def _load_bank_holidays(country: str = "UK") -> set:
    """Fetch bank holidays for one country; cache locally.
    UK: England & Wales calendar via gov.uk. Ireland: Nager.Date public API (IE)."""
    cache_path = _BANK_HOLIDAY_CACHE.get(country, _BANK_HOLIDAY_CACHE["UK"])
    today = date.today()
    cached_dates = set()

    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                raw = json.load(f)
            cached_dates = {date.fromisoformat(d) for d in raw}
            if any(d > today for d in cached_dates):
                return cached_dates          # cache is still valid
        except Exception:
            pass

    try:
        if country == "Ireland":
            date_strs = []
            for year in (today.year, today.year + 1):
                resp = requests.get(
                    f"https://date.nager.at/api/v3/PublicHolidays/{year}/IE", timeout=10)
                resp.raise_for_status()
                date_strs.extend(e["date"] for e in resp.json())
        else:
            resp = requests.get("https://www.gov.uk/bank-holidays.json", timeout=10)
            resp.raise_for_status()
            events = resp.json().get("england-and-wales", {}).get("events", [])
            date_strs = [e["date"] for e in events]

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(date_strs, f)
        log.info("%s bank holidays refreshed (%d dates)", country, len(date_strs))
        return {date.fromisoformat(d) for d in date_strs}
    except Exception as exc:
        log.warning("Could not fetch %s bank holidays: %s — using cached", country, exc)
        return cached_dates


_BANK_HOLIDAYS: dict = {}   # {country: set(dates)} — populated once in main()


def _is_bank_holiday(d: date, country: str = "UK") -> bool:
    return d in _BANK_HOLIDAYS.get(country, _BANK_HOLIDAYS.get("UK", set()))


def add_working_days(d: date, n: int, country: str = "UK") -> date:
    """Return d + n working days, skipping weekends and that country's bank holidays."""
    current = d
    added = 0
    while added < n:
        current += timedelta(days=1)
        if current.weekday() < 5 and not _is_bank_holiday(current, country):
            added += 1
    return current


def next_followup_date(send_date: date, country: str = "UK") -> date:
    """5 working days from one send to the next (Mon → Mon next week)."""
    return add_working_days(send_date, FOLLOWUP_GAP_WORKING_DAYS, country)


# ── Window checks ─────────────────────────────────────────────────────────────

def _in_window(now: datetime, window: tuple) -> bool:
    (sh, sm), (eh, em) = window
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now < end


def get_run_mode() -> str:
    """
    Returns:
      'followup_or_fresh'  — any active window (followups first, fresh fallback)
      'skip'               — outside all windows or weekend

    Bank holidays are NOT checked here — UK and Ireland rows share this window,
    and a holiday in one country shouldn't halt sends to the other. Per-row
    holiday filtering happens in process_sender() based on each row's Country.
    """
    now     = datetime.now(pytz.timezone(TIMEZONE))
    weekday = now.weekday()   # 0=Mon … 6=Sun

    if weekday >= 5:
        return "skip"

    if weekday == 4:   # Friday — followups first, fresh fallback
        return "followup_or_fresh" if _in_window(now, FRIDAY_WINDOW) else "skip"

    # Mon–Thu
    if _in_window(now, MORNING_WINDOW):
        return "followup_or_fresh"
    return "skip"


# ── Per-sender processor ──────────────────────────────────────────────────────

def _apply_uk_fresh_pause(sheet: SheetAgent, eligible: list, sender_name: str) -> list:
    """
    While PAUSE_UK_FRESH_SENDS is on, drop UK rows from a fresh-send candidate
    list as long as this sender still has UK followups or Ireland fresh/followups
    queued — self-clears (returns eligible unchanged) once none of those remain.
    """
    if not PAUSE_UK_FRESH_SENDS:
        return eligible
    work = sheet.get_work_status()
    if not any(work.values()):
        return eligible
    filtered = [r for r in eligible if (r.get("country") or "UK") != "UK"]
    if len(filtered) != len(eligible):
        log.info("[%s] UK fresh paused (still queued: %s) — skipped %d UK row(s)",
                  sender_name, ", ".join(k for k, v in work.items() if v),
                  len(eligible) - len(filtered))
    return filtered


def process_sender(sender: dict, mode: str):
    name = sender["email"]
    log.info("[%s] Run mode: %s", name, mode)

    try:
        sheet = SheetAgent(sender)
    except Exception as exc:
        log.error("[%s] Sheet init failed: %s", name, exc)
        return

    daily_limit = DAILY_LIMIT_FRIDAY if mode == "followup" else DAILY_LIMIT_WEEKDAY
    today_count = sheet.get_today_send_count()
    if today_count >= daily_limit:
        log.info("[%s] Daily limit reached (%d/%d)", name, today_count, daily_limit)
        return

    today = datetime.now(pytz.timezone(TIMEZONE)).date()

    def _skip_holiday_rows(rows):
        kept = [r for r in rows if not _is_bank_holiday(today, r.get("country") or "UK")]
        if len(kept) != len(rows):
            log.info("[%s] %d row(s) skipped — bank holiday in their country today",
                      name, len(rows) - len(kept))
        return kept

    # Resolve eligible rows based on mode
    # 'followup_or_fresh': try followups first; if none, fall back to fresh
    if mode == "followup_or_fresh":
        eligible = _skip_holiday_rows(sheet.get_eligible_rows(followups_only=True))
        effective_mode = "followup"
        if not eligible:
            eligible = _skip_holiday_rows(sheet.get_eligible_rows(fresh_only=True))
            effective_mode = "fresh"
            log.info("[%s] No followups due — falling back to fresh", name)
    else:
        eligible = _skip_holiday_rows(sheet.get_eligible_rows(
            fresh_only     = (mode == "fresh"),
            followups_only = (mode == "followup"),
        ))
        effective_mode = mode

    if effective_mode == "fresh":
        eligible = _apply_uk_fresh_pause(sheet, eligible, name)

    if not eligible:
        log.info("[%s] No eligible rows for mode=%s", name, mode)
        return

    # Tier-aware selection for fresh sends; followups take the first due row
    if effective_mode == "fresh":
        tier_counts = sheet.get_today_tier_counts()
        row = None
        for candidate in eligible:
            tier = candidate.get("tier", "5")
            if tier_counts.get(tier, 0) < DAILY_PER_TIER:
                row = candidate
                break
        if row is None:
            row = eligible[0]
            log.info("[%s] Overflow slot — tier %s (%s)",
                     name, row.get("tier", "?"), row.get("company_name", ""))
    else:
        row = eligible[0]

    seq              = row["sequence_step"]
    recipient        = row["recipient_email"]
    company          = row["company_name"]
    company_website  = row["company_website"]
    existing_thread  = row.get("thread_id", "")
    country          = row.get("country") or "UK"

    log.info("[%s] Row %d | %s | %s | seq %d | tier %s | country %s",
             name, row["row_number"], recipient, company, seq, row.get("tier", "?"), country)

    # Research (initial email only)
    if seq == 0:
        log.info("[%s] Researching %s", name, company)
        try:
            job = find_matching_role(company_website, company, country=country)
        except Exception as exc:
            log.error("[%s] Research failed for %s: %s", name, company, exc)
            job = {
                "role_title": "Open Application",
                "role_description": "",
                "is_open_application": True,
                "careers_url": company_website,
                "role_url":    company_website,
            }
    else:
        job = {
            "role_title":        row.get("role_applied", "Open Application"),
            "role_description":  "",
            "is_open_application": not bool(row.get("role_applied")),
            "careers_url":       company_website,
            "role_url":          company_website,
        }

    # Write email
    log.info("[%s] Writing email (seq %d) for %s", name, seq, recipient)
    try:
        email = write_email(
            row=row,
            job=job,
            sequence_step=seq,
            sender_name=sender["name"],
            sender_email=sender["email"],
            cv_path=sender["cv_path"],
        )
    except Exception as exc:
        log.error("[%s] Email writing failed for %s: %s", name, recipient, exc)
        return

    # CV attachment: fresh emails get a role-tailored CV (generated to a temp dir —
    # the sent Gmail message is the durable copy, nothing persists in the project
    # folder); followups reattach the exact same file sent in the initial email,
    # fetched from that thread, rather than generating a new one. Falls back to the
    # static master CV whenever tailoring/fetch doesn't produce something usable.
    gmail = GmailAgent(sender)
    cv_temp_dir = None
    cv_path = sender["cv_path"]
    cv_bytes = None
    cv_filename = ""

    if seq == 0:
        cv_temp_dir = tempfile.mkdtemp(prefix="cv_tailor_")
        try:
            tailored_path = tailor_cv(row=row, job=job, row_number=row["row_number"], out_dir=cv_temp_dir)
            if tailored_path:
                cv_path = tailored_path
                log.info("[%s] Using tailored CV for %s", name, recipient)
            else:
                log.info("[%s] Using master CV for %s (no tailoring)", name, recipient)
        except Exception as exc:
            log.warning("[%s] CV tailoring failed for %s, using master CV: %s", name, recipient, exc)
    elif existing_thread:
        fetched_name, fetched_bytes = gmail.get_first_attachment(existing_thread)
        if fetched_bytes:
            cv_bytes, cv_filename = fetched_bytes, fetched_name
            log.info("[%s] Reattaching initial CV for followup to %s", name, recipient)
        else:
            log.info("[%s] Could not fetch initial CV for %s, using master CV", name, recipient)

    # Send — reply in existing thread for followups
    log.info("[%s] Sending to %s | subject: %s | reply_thread: %s",
             name, recipient, email["subject"], existing_thread or "new")
    try:
        thread_id = gmail.send_email(
            to=recipient,
            subject=email["subject"],
            body_html=email["body_html"],
            body_plain=email["body_plain"],
            cv_path=cv_path,
            cv_bytes=cv_bytes,
            cv_filename=cv_filename,
            reply_to_thread_id=existing_thread if seq > 0 else "",
        )
    except Exception as exc:
        log.error("[%s] Send failed for %s: %s", name, recipient, exc)

        if _is_invalid_recipient_error(exc):
            repaired = _repair_invalid_recipient(recipient, company_website, row.get("first_name", ""))
            if not repaired:
                log.error("[%s] %s is invalid and couldn't be auto-repaired — marking bounced",
                           name, recipient)
                sheet.mark_bounced(row["row_number"])
                return
            log.warning("[%s] %s looks malformed — retrying as %s", name, recipient, repaired)
            try:
                thread_id = gmail.send_email(
                    to=repaired,
                    subject=email["subject"],
                    body_html=email["body_html"],
                    body_plain=email["body_plain"],
                    cv_path=cv_path,
                    cv_bytes=cv_bytes,
                    cv_filename=cv_filename,
                    reply_to_thread_id=existing_thread if seq > 0 else "",
                )
            except Exception as exc2:
                log.error("[%s] Retry with repaired address %s failed too: %s", name, repaired, exc2)
                sheet.mark_bounced(row["row_number"])
                return
            sheet.fix_recipient_email(row["row_number"], recipient, repaired)
            log.info("[%s] Repaired and sent: %s -> %s", name, recipient, repaired)
            recipient = repaired
        else:
            sent_subject = email["subject"]
            if seq > 0 and existing_thread and not sent_subject.lower().startswith("re:"):
                sent_subject = f"Re: {sent_subject}"
            thread_id = ""
            for attempt, delay in enumerate((0, 5, 10)):
                if delay:
                    time.sleep(delay)
                thread_id = gmail.find_recent_sent(recipient, sent_subject)
                if thread_id:
                    break
                log.warning(
                    "[%s] find_recent_sent found nothing for %s on attempt %d — "
                    "Gmail search index may still be catching up", name, recipient, attempt + 1
                )
            if thread_id:
                log.warning(
                    "[%s] %s actually went out despite the error (thread %s) — "
                    "recording it instead of resending", name, recipient, thread_id
                )
            else:
                return
    finally:
        if cv_temp_dir:
            shutil.rmtree(cv_temp_dir, ignore_errors=True)

    # Compute next followup date using working-day logic (before mark_sent)
    now_uk = datetime.now(pytz.timezone(TIMEZONE))
    followup_dt = next_followup_date(now_uk.date(), country)

    # Update sheet
    try:
        sheet.mark_sent(
            row_number=row["row_number"],
            sequence_step=seq,
            role_applied=job["role_title"] if seq == 0 else "",
            thread_id=thread_id if seq == 0 else "",   # only store on initial send
            next_followup_date=followup_dt,
            recipient_email=recipient,
        )
    except Exception as exc:
        log.error("[%s] Sheet update failed for row %d: %s", name, row["row_number"], exc)
        return

    log.info("[%s] Done — seq %d sent to %s @ %s (thread %s)",
             name, seq + 1, recipient, company, thread_id)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _BANK_HOLIDAYS
    _BANK_HOLIDAYS = {
        "UK":      _load_bank_holidays("UK"),
        "Ireland": _load_bank_holidays("Ireland"),
    }

    mode = get_run_mode()
    if mode == "skip":
        now = datetime.now(pytz.timezone(TIMEZONE))
        log.info("Outside send window (%s %s) — skipping",
                 now.strftime("%A"), now.strftime("%H:%M %Z"))
        return

    log.info("=== Email runner | mode=%s | %d senders ===", mode, len(SENDERS))

    pool = ThreadPoolExecutor(max_workers=len(SENDERS))
    futures = {pool.submit(process_sender, s, mode): s["email"] for s in SENDERS}
    # Normal runs finish in well under 30s, but CV tailoring can take up to
    # _MAX_ATTEMPTS (4) shrink retries at ~25-30s each — give that room rather
    # than killing a sender mid-tailor. Still far under the 15-min cron interval.
    done, pending = wait(futures, timeout=300)

    for future in done:
        sender_email = futures[future]
        try:
            future.result()
        except Exception as exc:
            log.error("Unhandled error for %s: %s", sender_email, exc)

    if pending:
        for future in pending:
            log.error("[%s] Sender processing exceeded 300s — abandoning to release the lock", futures[future])
        pool.shutdown(wait=False)
        # A stuck thread (e.g. a hung network call) would otherwise block process exit
        # forever and hold email_runner.lock — force-exit rather than wait on it.
        os._exit(1)

    pool.shutdown(wait=False)
    log.info("=== Email runner complete ===")

    # Sort all sheets (next followup dates updated by sends above)
    log.info("--- Sorting sheets ---")
    _sort_ex = ThreadPoolExecutor(max_workers=1)
    _sort_future = _sort_ex.submit(sort_all_sheets)
    try:
        _sort_future.result(timeout=60)
    except TimeoutError:
        log.warning("Sheet sort timed out after 60s — skipping")
    except Exception as exc:
        log.warning("Sheet sort failed: %s", exc)
    finally:
        _sort_ex.shutdown(wait=False)

    # Run reply checker
    log.info("--- Running reply checker ---")
    try:
        subprocess.run(
            [sys.executable, os.path.join(_BASE_DIR, "reply_checker.py")],
            timeout=300,
        )
    except Exception as exc:
        log.warning("Reply checker failed: %s", exc)

    # Mark contacts "not interested" once their final followup checkpoint has
    # passed with still no reply (reply_checker above gets first look, so a
    # same-cycle reply isn't clobbered)
    log.info("--- Expiring completed sequences ---")
    for sender in SENDERS:
        try:
            expired = SheetAgent(sender).expire_completed_sequences()
            if expired:
                log.info("[%s] Marked %d row(s) not interested", sender["email"], expired)
        except Exception as exc:
            log.warning("[%s] Expire completed sequences failed: %s", sender["email"], exc)

    # Update email_logs sheet
    log.info("--- Updating email logs ---")
    try:
        subprocess.run(
            [sys.executable, os.path.join(_BASE_DIR, "log_writer.py")],
            timeout=60,
        )
    except Exception as exc:
        log.warning("Log writer failed: %s", exc)


if __name__ == "__main__":
    main()
