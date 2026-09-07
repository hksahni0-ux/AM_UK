#!/usr/bin/env python3
"""
sheet_sort.py — Sorts all 3 sheets by reading, sorting in Python, and writing
back from row 2 (no clear) so formatting and dropdowns are always preserved.

Sort order:
  1. Tier (1 → 5)
  2. Next Followup date (oldest first; blanks — fresh contacts — go to bottom)
  3. Status (blank → followup initiated → discussion in progress →
             interview arranged → selected → not selected → not interested)
"""

import sys
from collections import Counter
from datetime import date, datetime
from typing import List

sys.path.insert(0, ".")

import gspread

from config.settings import SENDERS, SPREADSHEET_ID
from agents.sheet_agent import _load_creds

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_COL     = "Tier"
CATEGORY_COL = "Category"

TIER_LABELS = {
    "1": "R&D Institute",
    "2": "Aerospace / Defence",
    "3": "Major AM Brand",
    "4": "AM Startup",
    "5": "General",
}

STATUS_RANK = {
    "":                        0,
    "followup initiated":      1,
    "sequence complete":       2,
    "discussion in progress":  3,
    "interview arranged":      4,
    "selected":                5,
    "not selected":            6,
    "not interested":          7,
}

_DATE_FAR_FUTURE = date(9999, 12, 31)   # blank dates sort last


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    if not s or not s.strip():
        return _DATE_FAR_FUTURE
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return _DATE_FAR_FUTURE


def _sort_key(row: List[str], tier_idx: int, next_followup_idx: int, status_idx: int):
    tier   = int(row[tier_idx].strip()) if tier_idx < len(row) and row[tier_idx].strip().isdigit() else 9
    dt     = _parse_date(row[next_followup_idx] if 0 <= next_followup_idx < len(row) else "")
    status = row[status_idx].strip().lower() if 0 <= status_idx < len(row) else ""
    return (tier, dt, STATUS_RANK.get(status, 8))


def _get_or_add_col(ws, col_name: str) -> int:
    """Return 1-based column index, creating the column if it doesn't exist."""
    headers = ws.row_values(1)
    if col_name in headers:
        return headers.index(col_name) + 1
    new_col = len(headers) + 1
    if ws.col_count < new_col:
        ws.resize(cols=new_col)
    ws.update_cell(1, new_col, col_name)
    return new_col


# ── Main processor ────────────────────────────────────────────────────────────

def sort_sheet(ws, sheet_name: str, creds=None):
    print(f"\n  Sorting '{sheet_name}'…")

    # Ensure structural columns exist
    tier_col_idx     = _get_or_add_col(ws, TIER_COL)
    category_col_idx = _get_or_add_col(ws, CATEGORY_COL)
    _get_or_add_col(ws, "Thread ID")

    all_values = ws.get_all_values()
    if len(all_values) < 2:
        print("    No data rows — skipping.")
        return

    headers   = all_values[0]
    num_cols  = len(headers)

    # Pad every row to header width so column indices are always valid
    data_rows = [
        (list(r) + [""] * num_cols)[:num_cols]
        for r in all_values[1:]
    ]

    tier_idx          = tier_col_idx - 1
    category_idx      = category_col_idx - 1
    next_followup_idx = headers.index("Next Followup") if "Next Followup" in headers else -1
    status_idx        = headers.index("Status")        if "Status"        in headers else -1

    # Refresh Category values in memory before sorting
    for row in data_rows:
        tier_val = row[tier_idx].strip() if tier_idx < len(row) else ""
        row[category_idx] = TIER_LABELS.get(tier_val, "")

    # Sort with custom comparator (blank dates → bottom via _DATE_FAR_FUTURE)
    data_rows.sort(key=lambda r: _sort_key(r, tier_idx, next_followup_idx, status_idx))

    # Write sorted rows back from A2 — no clear(), so dropdowns are preserved
    ws.update(data_rows, range_name="A2", value_input_option="USER_ENTERED")

    tier_counts = Counter(row[tier_idx].strip() for row in data_rows)
    print(f"    Done — {len(data_rows)} rows sorted.")
    for t in sorted(tier_counts):
        print(f"      Tier {t} ({TIER_LABELS.get(t, 'Unknown')}): {tier_counts[t]}")


def main():
    print("Auto-sorting all 3 sheets…")

    owner_creds = _load_creds(SENDERS[0]["token_file"])
    gc = gspread.authorize(owner_creds)
    ss = gc.open_by_key(SPREADSHEET_ID)

    for sender in SENDERS:
        ws = ss.worksheet(sender["sheet_name"])
        sort_sheet(ws, sender["sheet_name"])

    print("\nAll sheets sorted.")


if __name__ == "__main__":
    main()
