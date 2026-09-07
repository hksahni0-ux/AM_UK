"""
SheetAgent: reads eligible rows from Google Sheets and writes back status updates.
"""

import logging
from datetime import date, datetime
from typing import Optional, List

import pytz
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config.settings import (
    SPREADSHEET_ID, COLUMNS, GOOGLE_SCOPES, TIMEZONE,
    STATUS_BLANK, STATUS_FOLLOWUP_INITIATED, STATUS_DISCUSSION,
    STATUS_NOT_INTERESTED, STATUS_BOUNCED, STATUS_NO_LONGER_WITH_COMPANY,
    REPLY_STATUS_RECEIVED,
    MAX_SEQUENCE, DAILY_PER_TIER,
    DAILY_LIMIT_WEEKDAY, DAILY_LIMIT_FRIDAY,
    SENDERS,
)

log = logging.getLogger(__name__)


def _uk_now() -> datetime:
    return datetime.now(pytz.timezone(TIMEZONE))


def _load_creds(token_file: str) -> Credentials:
    creds = Credentials.from_authorized_user_file(token_file, GOOGLE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(f"Invalid credentials in {token_file}. Run setup_auth.py.")
    return creds


class SheetAgent:
    def __init__(self, sender: dict):
        self.sender = sender
        owner_creds = _load_creds(SENDERS[0]["token_file"])
        gc = gspread.authorize(owner_creds)
        gc.set_timeout(30)  # gspread ignores socket.setdefaulttimeout() — hangs forever otherwise
        self.ws = gc.open_by_key(SPREADSHEET_ID).worksheet(sender["sheet_name"])
        self._headers = None

    # ── internal helpers ──────────────────────────────────────────────────────

    def _get_headers(self) -> List[str]:
        if self._headers is None:
            self._headers = self.ws.row_values(1)
        return self._headers

    def _col_index(self, key: str) -> Optional[int]:
        col_name = COLUMNS.get(key)
        if not col_name:
            return None
        headers = self._get_headers()
        return headers.index(col_name) if col_name in headers else None

    def _get_val(self, row: List, key: str) -> str:
        idx = self._col_index(key)
        if idx is None:
            return ""
        try:
            return row[idx].strip()
        except IndexError:
            return ""

    def _parse_date(self, s: str) -> Optional[date]:
        if not s:
            return None
        s = s.strip()
        for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def _parse_action_date(self, s: str) -> Optional[date]:
        """Parse a 'last_action_date' value, e.g. '4 September 2026 09:45:31'.
        Sheets auto-formats the day without zero-padding on write-back, so this
        must not rely on exact string matching against a strftime'd prefix."""
        if not s:
            return None
        parts = s.strip().split(" ")
        if len(parts) < 3:
            return None
        try:
            return datetime.strptime(" ".join(parts[:3]), "%d %B %Y").date()
        except ValueError:
            return None

    def _all_rows(self) -> List[dict]:
        records = self.ws.get_all_values()
        if len(records) < 2:
            return []
        return [{"row_number": i, "data": row} for i, row in enumerate(records[1:], start=2)]

    def _build_row_dict(self, row: List, row_num: int, seq: int) -> dict:
        first = self._get_val(row, "first_name")
        last = self._get_val(row, "last_name")
        full_name = f"{first} {last}".strip() or "Hiring Manager"
        return {
            "row_number": row_num,
            "sequence_step": seq,
            "recipient_name": full_name,
            "first_name": first,
            "recipient_email": self._get_val(row, "recipient_email"),
            "recipient_job_title": self._get_val(row, "recipient_job_title"),
            "company_name": self._get_val(row, "company_name"),
            "company_website": self._get_val(row, "company_website"),
            "company_industry": self._get_val(row, "company_industry"),
            "comments":           self._get_val(row, "comments"),
            "role_applied":       self._get_val(row, "role_applied"),
            "person_linkedin_url": self._get_val(row, "person_linkedin_url"),
            "tier":               self._get_val(row, "tier") or "5",
            "category":           self._get_val(row, "category"),
            "thread_id":          self._get_val(row, "thread_id"),
            "country":            self._get_val(row, "country") or "UK",
        }

    # ── public API ────────────────────────────────────────────────────────────

    def get_eligible_rows(self, fresh_only: bool = False, followups_only: bool = False) -> List[dict]:
        """
        Return rows ready to receive an email, in sheet order.
          fresh_only=True     → initial emails only  (Mon–Thu)
          followups_only=True → followup emails only  (Friday)
          neither             → both
        Skips rows in Discussion, Not Interested, Bounced, or Reply Received.
        """
        today = _uk_now().date()
        eligible = []

        for item in self._all_rows():
            row     = item["data"]
            row_num = item["row_number"]

            status       = self._get_val(row, "status")
            reply_status = self._get_val(row, "reply_status")
            seq_raw      = self._get_val(row, "sequence_step")
            try:
                seq = max(0, int(seq_raw))
            except (ValueError, TypeError):
                seq = 0
            followup_date = self._parse_date(self._get_val(row, "next_followup_date"))
            email        = self._get_val(row, "recipient_email")

            if not email:
                continue
            if status in (STATUS_DISCUSSION, STATUS_NOT_INTERESTED, STATUS_BOUNCED,
                          STATUS_NO_LONGER_WITH_COMPANY):
                continue
            if reply_status == REPLY_STATUS_RECEIVED:
                continue

            is_fresh    = status == STATUS_BLANK and seq == 0
            is_followup = (
                status == STATUS_FOLLOWUP_INITIATED
                and seq < MAX_SEQUENCE
                and followup_date is not None
                and followup_date <= today
            )

            if fresh_only    and not is_fresh:    continue
            if followups_only and not is_followup: continue
            if not is_fresh and not is_followup:   continue

            eligible.append(self._build_row_dict(row, row_num, seq))

        return eligible

    def get_work_status(self) -> dict:
        """
        Single-pass scan of this sender's sheet for the UK-fresh-pause gate:
        {"uk_followups": bool, "ireland_fresh": bool, "ireland_followups": bool}
        — whether each category still has any row outstanding (mid-sequence or
        not-yet-contacted), regardless of whether it's due today. Unlike
        get_eligible_rows(), this ignores next_followup_date entirely — it's
        asking "is this category of work finished yet?", not "is it due now?".
        """
        result = {"uk_followups": False, "ireland_fresh": False, "ireland_followups": False}

        for item in self._all_rows():
            row = item["data"]
            status       = self._get_val(row, "status")
            reply_status = self._get_val(row, "reply_status")
            email        = self._get_val(row, "recipient_email")

            if not email:
                continue
            if status in (STATUS_DISCUSSION, STATUS_NOT_INTERESTED, STATUS_BOUNCED,
                          STATUS_NO_LONGER_WITH_COMPANY):
                continue
            if reply_status == REPLY_STATUS_RECEIVED:
                continue

            country = self._get_val(row, "country") or "UK"
            seq_raw = self._get_val(row, "sequence_step")
            try:
                seq = max(0, int(seq_raw))
            except (ValueError, TypeError):
                seq = 0

            is_fresh    = status == STATUS_BLANK and seq == 0
            is_followup = status == STATUS_FOLLOWUP_INITIATED and seq < MAX_SEQUENCE

            if country == "UK" and is_followup:
                result["uk_followups"] = True
            elif country == "Ireland" and is_fresh:
                result["ireland_fresh"] = True
            elif country == "Ireland" and is_followup:
                result["ireland_followups"] = True

            if all(result.values()):
                break

        return result

    def get_today_send_count(self) -> int:
        today = _uk_now().date()
        return sum(
            1 for item in self._all_rows()
            if self._parse_action_date(self._get_val(item["data"], "last_action_date")) == today
        )

    def get_today_tier_counts(self) -> dict:
        """Return {tier_str: count} of emails sent today, per tier."""
        today = _uk_now().date()
        counts: dict = {}
        for item in self._all_rows():
            row = item["data"]
            if self._parse_action_date(self._get_val(row, "last_action_date")) != today:
                continue
            tier = self._get_val(row, "tier") or "5"
            counts[tier] = counts.get(tier, 0) + 1
        return counts

    def _live_row_number(self, recipient_email: str, fallback: int) -> int:
        """Re-fetch the sheet to find the current row for recipient_email.
        Guards against sort_sheet shifting rows between fetch and write."""
        try:
            records = self.ws.get_all_values()
            if not records:
                return fallback
            headers = records[0]
            email_col = headers.index(COLUMNS["recipient_email"]) if COLUMNS.get("recipient_email") in headers else -1
            if email_col < 0:
                return fallback
            for i, row in enumerate(records[1:], start=2):
                if email_col < len(row) and row[email_col].strip().lower() == recipient_email.strip().lower():
                    return i
        except Exception:
            pass
        return fallback

    def mark_sent(self, row_number: int, sequence_step: int,
                  role_applied: str = "", thread_id: str = "",
                  next_followup_date: Optional[date] = None,
                  recipient_email: str = ""):
        """Update sheet after sending. sequence_step is the step just sent (0,1,2).
        next_followup_date should be pre-computed by email_runner using working-day logic.
        Always schedules a next-followup date, even after the final step — that
        date isn't used to send another email (seq >= MAX_SEQUENCE keeps the row
        out of get_eligible_rows), only as the "no reply by" checkpoint that
        expire_completed_sequences() later uses to mark the row not interested."""
        now = _uk_now()
        new_seq = sequence_step + 1

        new_status    = STATUS_FOLLOWUP_INITIATED
        next_followup = next_followup_date.strftime("%d %b %Y") if next_followup_date else ""

        updates = {
            "status":             new_status,
            "sequence_step":      str(new_seq),
            "next_followup_date": next_followup,
            "last_action_date":   now.strftime("%d %B %Y %H:%M:%S"),
        }
        if role_applied:
            updates["role_applied"] = role_applied
        if thread_id:
            updates["thread_id"] = thread_id

        actual_row = self._live_row_number(recipient_email, row_number) if recipient_email else row_number
        if actual_row != row_number:
            log.warning("Row shifted after sort: expected %d, writing to %d (%s)",
                        row_number, actual_row, recipient_email)
        self._write_updates(actual_row, updates)
        log.info("Row %d: seq %d→%d status=%s next=%s thread=%s",
                 actual_row, sequence_step, new_seq, new_status, next_followup, thread_id)

    def mark_bounced(self, row_number: int):
        now = _uk_now()
        self._write_updates(row_number, {
            "status": STATUS_BOUNCED,
            "last_action_date": now.strftime("%d %B %Y %H:%M:%S"),
        })
        log.info("Row %d: email bounced → bounced", row_number)

    def mark_reply_received(self, row_number: int):
        now = _uk_now()
        self._write_updates(row_number, {
            "status": STATUS_DISCUSSION,
            "reply_status": REPLY_STATUS_RECEIVED,
            "last_action_date": now.strftime("%d %B %Y %H:%M:%S"),
        })
        log.info("Row %d: reply received → Discussion in Progress", row_number)

    def mark_not_interested(self, row_number: int):
        now = _uk_now()
        self._write_updates(row_number, {
            "status": STATUS_NOT_INTERESTED,
            "next_followup_date": "",
            "last_action_date": now.strftime("%d %B %Y %H:%M:%S"),
        })
        log.info("Row %d: sequence complete, no reply by followup date → not interested", row_number)

    def expire_completed_sequences(self) -> int:
        """Find rows that finished all MAX_SEQUENCE sends, got no reply, and
        whose next-followup checkpoint has passed — mark them not interested.
        Returns the number of rows updated."""
        today = _uk_now().date()
        count = 0
        for item in self._all_rows():
            row = item["data"]
            status = self._get_val(row, "status")
            if status != STATUS_FOLLOWUP_INITIATED:
                continue
            if self._get_val(row, "reply_status") == REPLY_STATUS_RECEIVED:
                continue
            seq_raw = self._get_val(row, "sequence_step")
            try:
                seq = max(0, int(seq_raw))
            except (ValueError, TypeError):
                seq = 0
            if seq < MAX_SEQUENCE:
                continue
            followup_date = self._parse_date(self._get_val(row, "next_followup_date"))
            if followup_date is None or followup_date > today:
                continue
            self.mark_not_interested(item["row_number"])
            count += 1
        return count

    def mark_no_longer_with_company(self, row_number: int):
        now = _uk_now()
        self._write_updates(row_number, {
            "status": STATUS_NO_LONGER_WITH_COMPANY,
            "last_action_date": now.strftime("%d %B %Y %H:%M:%S"),
        })
        log.info("Row %d: contact no longer with company → no longer with company", row_number)

    def mark_ooo(self, row_number: int, next_followup: date, recipient_email: str = ""):
        """OOO detected — push next followup to return_date + 2 working days. Status unchanged."""
        actual_row = self._live_row_number(recipient_email, row_number) if recipient_email else row_number
        if actual_row != row_number:
            log.warning("OOO mark: row shifted %d → %d (%s)", row_number, actual_row, recipient_email)
        self._write_updates(actual_row, {
            "next_followup_date": next_followup.strftime("%d %b %Y"),
            "comments": f"OOO — followup from {next_followup.strftime('%d %b %Y')}",
        })
        log.info("Row %d: OOO → next followup %s", actual_row, next_followup)

    def _write_updates(self, row_number: int, updates: dict):
        headers = self._get_headers()
        cells = []
        for key, value in updates.items():
            col_name = COLUMNS.get(key)
            if col_name and col_name in headers:
                col_idx = headers.index(col_name) + 1
                cells.append(gspread.Cell(row_number, col_idx, value))
        if cells:
            self.ws.update_cells(cells, value_input_option="USER_ENTERED")

    def get_rows_for_reply_check(self, include_resolved: bool = False) -> List[dict]:
        """
        Return rows to poll for replies.
          include_resolved=False (default — cron cadence) → only rows still
            actively following up (status "followup initiated", seq>0, no
            reply received yet).
          include_resolved=True (on-demand deep sweep, e.g. `reply_checker.py
            --full`) → every row with a thread, including ones already marked
            not interested / discussion in progress — so a departure notice
            or reply missed while the row was still active still gets caught.
            Skips rows with nothing left to learn (bounced, or already marked
            "no longer with company").
        """
        rows = []
        for item in self._all_rows():
            row = item["data"]
            status = self._get_val(row, "status")
            reply_status = self._get_val(row, "reply_status")
            email = self._get_val(row, "recipient_email")
            thread_id = self._get_val(row, "thread_id")
            seq_raw = self._get_val(row, "sequence_step")
            try:
                seq = max(0, int(seq_raw))
            except (ValueError, TypeError):
                seq = 0

            if not email or not thread_id:
                continue

            if include_resolved:
                if status in (STATUS_BOUNCED, STATUS_NO_LONGER_WITH_COMPANY):
                    continue
            else:
                if not (seq > 0 and status == STATUS_FOLLOWUP_INITIATED
                        and reply_status != REPLY_STATUS_RECEIVED):
                    continue

            rows.append({
                "row_number": item["row_number"],
                "recipient_email": email,
                "thread_id": thread_id,
                "last_action_date": self._get_val(row, "last_action_date"),
                "next_followup_date": self._parse_date(self._get_val(row, "next_followup_date")),
                "comments": self._get_val(row, "comments"),
                "status": status,
                "reply_status": reply_status,
            })
        return rows
