#!/usr/bin/env python3
"""
log_writer.py — daily summary writer (run at end of day via cron).

Reads all 3 sender sheets and writes a summary to the 'email_logs' sheet:
  - Emails sent today, per account, broken down by tier
  - Replies received today, per account
"""

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

import pytz
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    SPREADSHEET_ID, COLUMNS, GOOGLE_SCOPES, TIMEZONE, SENDERS,
    STATUS_DISCUSSION, REPLY_STATUS_RECEIVED,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR  = os.path.join(_BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.FileHandler(os.path.join(_LOG_DIR, "log_writer.log"))],
)
log = logging.getLogger("log_writer")

LOG_SHEET_NAME = "email_logs"
TIERS = ["1", "2", "3", "4", "5"]

HEADER = (
    ["Date", "Account", "Tier 1", "Tier 2", "Tier 3", "Tier 4", "Tier 5",
     "Total Sent", "Replies Received"]
)


def _uk_now() -> datetime:
    return datetime.now(pytz.timezone(TIMEZONE))


def _load_creds(token_file: str) -> Credentials:
    creds = Credentials.from_authorized_user_file(token_file, GOOGLE_SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _col(headers: list, key: str) -> str:
    col_name = COLUMNS.get(key, "")
    idx = headers.index(col_name) if col_name in headers else -1
    return idx


def _get_val(row: list, idx: int) -> str:
    if idx < 0:
        return ""
    try:
        return row[idx].strip()
    except IndexError:
        return ""


def _parse_action_date(last_action_date: str):
    """Parse a 'last_action_date' value, e.g. '4 September 2026 20:08:13'.
    Sheets auto-formats the day without zero-padding on write-back, so this
    must not rely on exact string matching against a strftime'd prefix."""
    parts = last_action_date.strip().split(" ")
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(" ".join(parts[:3]), "%d %B %Y").date()
    except ValueError:
        return None


def collect_day_stats(target_date) -> dict:
    """
    Returns {account_name: {"tiers": {tier: count}, "replies": int}}
    for all rows whose last_action_date falls on target_date.
    """
    owner_creds = _load_creds(SENDERS[0]["token_file"])
    gc = gspread.authorize(owner_creds)

    stats = {}
    for sender in SENDERS:
        account = sender["sheet_name"]
        stats[account] = {"tiers": defaultdict(int), "replies": 0}

        ws = gc.open_by_key(SPREADSHEET_ID).worksheet(account)
        records = ws.get_all_values()
        if len(records) < 2:
            continue

        headers = records[0]
        col_status       = _col(headers, "status")
        col_reply_status = _col(headers, "reply_status")
        col_action_date  = _col(headers, "last_action_date")
        col_tier         = _col(headers, "tier")
        col_seq          = _col(headers, "sequence_step")

        for row in records[1:]:
            action_date = _get_val(row, col_action_date)
            if not action_date:
                continue
            if _parse_action_date(action_date) != target_date:
                continue

            status       = _get_val(row, col_status)
            reply_status = _get_val(row, col_reply_status)
            tier         = _get_val(row, col_tier) or "5"
            seq_raw      = _get_val(row, col_seq)
            try:
                seq = int(seq_raw)
            except ValueError:
                seq = 0

            # Reply received today
            if reply_status == REPLY_STATUS_RECEIVED and status == STATUS_DISCUSSION:
                stats[account]["replies"] += 1

            # Email sent today: seq > 0 and status is not purely reply-detected
            # A send always increments seq; reply detection does not change seq
            elif seq > 0 and status != STATUS_DISCUSSION:
                stats[account]["tiers"][tier] += 1

    return stats


def build_rows(date_str: str, stats: dict) -> list:
    """Build sheet rows for this date: one per account + one TOTAL row."""
    rows = []
    totals_tiers = defaultdict(int)
    totals_replies = 0

    for sender in SENDERS:
        account = sender["sheet_name"]
        s = stats[account]
        tier_counts = [s["tiers"].get(t, 0) for t in TIERS]
        total_sent = sum(tier_counts)
        replies = s["replies"]

        totals_replies += replies
        for t, c in zip(TIERS, tier_counts):
            totals_tiers[t] += c

        rows.append([date_str, account] + tier_counts + [total_sent, replies])

    total_tier_counts = [totals_tiers.get(t, 0) for t in TIERS]
    rows.append(
        [date_str, "TOTAL"] + total_tier_counts +
        [sum(total_tier_counts), totals_replies]
    )
    return rows


def write_log(date_str: str, new_rows: list):
    """Prepend today's rows to email_logs, replacing any existing rows for that date."""
    owner_creds = _load_creds(SENDERS[0]["token_file"])
    gc = gspread.authorize(owner_creds)
    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(LOG_SHEET_NAME)

    existing = ws.get_all_values()

    # Keep header + all rows NOT from today
    if existing and existing[0] == HEADER:
        kept = [r for r in existing[1:] if r and r[0] != date_str]
    else:
        kept = []

    # New content: header + today's rows (newest first) + older rows
    content = [HEADER] + new_rows + kept
    ws.clear()
    ws.update(range_name="A1", values=content)
    log.info("email_logs updated — %d rows for %s", len(new_rows), date_str)


def main():
    now = _uk_now()
    date_str = now.strftime("%d %B %Y")    # e.g. "06 August 2026" (display/sheet-key format)
    log.info("=== log_writer started for %s ===", date_str)

    stats = collect_day_stats(now.date())
    rows  = build_rows(date_str, stats)
    write_log(date_str, rows)

    # Quick console summary
    for row in rows:
        log.info("%s | %s | sent=%s | replies=%s", row[0], row[1], row[7], row[8])

    log.info("=== log_writer complete ===")


if __name__ == "__main__":
    main()
