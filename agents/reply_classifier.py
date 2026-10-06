"""
Shared LLM-based classification of inbound reply bodies — used by reply_checker.py
for both the routine cron poll and the on-demand `--full` deep sweep.

One reply body can mean one of three things — a real reply, an out-of-office
auto-responder, or a departure notice ("no longer with the company") — and all
three can arrive looking identical from the outside (same subject patterns, same
auto-responder mechanism), so classify_reply() decides all of it in a single call
instead of spreading the decision across separate checks that can independently
miss a signal (that gap previously let a departure notice sent as an "Automatic
reply" slip past because only the OOO branch ever saw it).
"""

import logging
import re
from datetime import date, timedelta
from typing import Optional

from agents import llm_client

log = logging.getLogger(__name__)

_LEFT_COMPANY_TRIGGERS = [
    "no longer with", "no longer works", "no longer working",
    "no longer employed", "no longer at", "no longer be",
    "has left the company", "has left the organisation", "has left the organization",
    "left the business", "is no longer", "does not work here", "doesn't work here",
    "no longer part of", "moved on from", "no longer be with",
    "no longer work", "has retired", "have retired",
]


def has_departure_keywords(body: str) -> bool:
    """
    Cheap pre-check: does this body contain explicit departure language at all?
    Used both to gate re-classification of an already-handled OOO (skip the LLM
    call when nothing new could have changed the verdict) and as a safety net
    against the model over-classifying a plain decline/redirect as 'left_company'
    — a small model did exactly that on the first version of this classifier.
    """
    lowered = body.lower()
    return any(trigger in lowered for trigger in _LEFT_COMPANY_TRIGGERS)


_CLASSIFY_TOOL = {
    "name": "classify_reply",
    "description": "Classify an email reply received to a job outreach email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["real_reply", "ooo", "left_company"],
                "description": (
                    "'real_reply' — the contact (or a colleague) replying personally about "
                    "the outreach, including declines, redirects, or requests for info. "
                    "'ooo' — an out-of-office / automatic-reply auto-responder with no "
                    "indication the contact has left the company. "
                    "'left_company' — explicitly states the contact has left, is no longer "
                    "employed at, or is no longer with the company (an automated notice, or "
                    "a colleague saying so on their behalf)."
                ),
            },
            "return_date": {
                "type": "string",
                "description": (
                    "Only when category is 'ooo' and a return/back date is mentioned: the "
                    "date in YYYY-MM-DD format. Resolve it relative to the date the email "
                    "was SENT: a date with no month (e.g. 'Friday the 2nd', 'back on the "
                    "14th') is the first such date on or after the sent date; a date with no "
                    "year is in the sent date's year (even if that is already in the past). "
                    "These emails are from UK/Ireland senders — read any "
                    "ambiguous numeric date (e.g. 10/08/26) as DD/MM/YY, not MM/DD/YY. If "
                    "the email also states a weekday (e.g. 'Monday 10/08/26'), use it to "
                    "confirm: pick whichever reading of the date actually falls on that "
                    "weekday. Omit entirely if category isn't 'ooo' or no date is given."
                ),
            },
        },
        "required": ["category"],
    },
}


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_MONTHS.update({m[:3]: n for m, n in list(_MONTHS.items())})
_MONTHS["sept"] = 9
# "Friday the 2nd", "Monday 12th October", "Tuesday 5 Jan" — weekday + day number,
# month optional. The weekday pins down which month a month-less date means.
_WEEKDAY_DATE_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r")\s*,?\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b(?![/.-]\d)"
    r"(?:\s+(?:of\s+)?(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b)?",
    re.IGNORECASE,
)


_EXPLICIT_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b|\b\d{1,2}[/.-]\d{1,2}\b",
    re.IGNORECASE,
)


def resolve_weekday_date(body: str, sent: date) -> Optional[date]:
    """
    The return date for an OOO that names it as weekday + day number ("Friday the
    2nd"), worked out in code: the first date from the sent date onward with that day
    number, that weekday (and that month, if given). A small model resolved "Friday
    the 2nd" — sent Wed 30 Sep — to 2 Nov, 6 Nov or 2 Jan on different runs, pushing a
    followup out by three months. None if the email has no such phrase or no date fits.
    """
    m = _WEEKDAY_DATE_RE.search(body)
    if not m:
        return None
    weekday = _WEEKDAYS.index(m.group(1).lower())
    day = int(m.group(2))
    month = _MONTHS.get((m.group(3) or "").lower())
    d = sent - timedelta(days=3)       # "back Friday the 2nd" can be sent on the 2nd itself
    for _ in range(400):
        if d.day == day and d.weekday() == weekday and (month is None or d.month == month):
            return d
        d += timedelta(days=1)
    return None


def classify_reply(body: str, today: Optional[date] = None, sent: Optional[date] = None) -> dict:
    """
    Classify a gathered reply body — callers should combine text from BOTH an
    in-thread reply and a broad auto-reply/OOO mailbox search before calling this,
    since a departure notice can arrive via either channel and looks identical to
    a genuine OOO by subject line alone.

    sent: the date the (latest) reply was sent — partial return dates are resolved
    against it, not against today, since an OOO can be read days after it arrived.

    Returns {"category": "real_reply"|"ooo"|"left_company", "return_date": date|None}.
    Falls back to real_reply on any LLM error or empty body, so a classification
    hiccup never silently drops or mishandles a genuine reply.
    """
    if not body.strip():
        return {"category": "real_reply", "return_date": None}

    today = today or date.today()
    sent = sent or today
    try:
        result = llm_client.complete_tool(
            "left_company_check",
            f"Today is {today.isoformat()}. The email was sent on {sent:%A %d %B %Y} "
            f"({sent.isoformat()}). Classify this email reply to a job outreach email.\n\n"
            f"Email:\n{body}",
            _CLASSIFY_TOOL,
            max_tokens=200,
        )
        category = result.get("category", "real_reply")
        if category not in ("real_reply", "ooo", "left_company"):
            category = "real_reply"

        if category == "left_company" and not has_departure_keywords(body):
            log.warning("Downgrading left_company classification without keyword match: %r", body[:200])
            category = "real_reply"

        return_date = None
        if category == "ooo":
            raw_date = result.get("return_date")
            if raw_date:
                try:
                    return_date = date.fromisoformat(raw_date)
                except ValueError:
                    pass
            # A model-invented far-off date with nothing in the text to back it up
            # (no month named, no numeric date) is worse than no date at all.
            if (return_date and (return_date - sent).days > 45
                    and not _EXPLICIT_MONTH_RE.search(body)):
                log.warning("Dropping OOO return date %s — nothing in the text names a month", return_date)
                return_date = None
            # A weekday + day number in the text is unambiguous — trust the calendar
            # over the model whenever both are available.
            resolved = resolve_weekday_date(body, sent)
            if resolved and resolved != return_date:
                log.info("OOO return date: model said %s, text says %s — using %s",
                         return_date, resolved, resolved)
                return_date = resolved

        return {"category": category, "return_date": return_date}
    except Exception as exc:
        log.warning("Reply classification failed: %s", exc)
        return {"category": "real_reply", "return_date": None}
