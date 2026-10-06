"""
Pulls contact details out of inbound replies, for reply_checker.py:
  • the author's own mobile number from their signature or OOO text → Mobile column
  • any colleague the email points us to ("please contact nicola.jeffery@...") or a
    colleague replying in the contact's place → a new row under the original contact,
    with the LinkedIn URL and job title looked up via Exa when the email doesn't give them

Regexes decide whether there is anything new to extract at all (so a long-running OOO
re-read every cycle costs no LLM call), and every value the LLM returns must literally
appear in the email before it is used — a small model invents plausible-looking
details otherwise.
"""

import logging
import re
from typing import Callable, Dict, List, Set, Tuple

from agents import llm_client
from agents.auto_responder import _QUOTE_MARKERS, _normalise_text
from config.settings import SENDERS, EXA_API_KEY

try:
    from exa_py import Exa as _Exa
except ImportError:
    _Exa = None

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+|00)?[\d(][\d \t().-]{8,}\d")

_OWN_EMAILS = {s["email"].lower() for s in SENDERS}

# Shared mailboxes aren't a person to add as a contact row.
_GENERIC_LOCAL_PARTS = {
    "info", "sales", "hr", "careers", "career", "jobs", "recruitment", "recruiting",
    "enquiries", "enquiry", "inquiries", "admin", "office", "contact", "hello",
    "support", "reception", "accounts", "noreply", "no-reply", "donotreply",
    "marketing", "service", "team", "mail", "postmaster", "mailer-daemon", "help",
    "people", "talent", "general", "operations", "quality", "engineering",
    "it", "orders", "tooling", "technical", "purchasing", "finance", "buying",
    "projects", "design", "production", "logistics", "dispatch", "estimating",
}

# ── Text helpers ──────────────────────────────────────────────────────────────

def own_text(body: str) -> str:
    """The message minus quoted history — but WITH the signature, which is where the
    phone numbers are. Our own quoted email (with our own number) is cut off here."""
    text = _normalise_text(body)
    m = _QUOTE_MARKERS.search(text)
    return (text[:m.start()] if m else text).strip()


def normalise_mobile(raw: str) -> str:
    """UK / Ireland mobile → sheet format ('447500441165', '353871234567'), else ''.
    Landlines and other countries are ignored — the column is for mobiles."""
    d = re.sub(r"\D", "", (raw or "").replace("(0)", ""))
    if d.startswith("00"):
        d = d[2:]
    if re.fullmatch(r"4407\d{9}", d) or re.fullmatch(r"35308[3-9]\d{7}", d):
        d = d[:2] + d[3:] if d.startswith("44") else d[:3] + d[4:]
    if re.fullmatch(r"07\d{9}", d):
        d = "44" + d[1:]
    elif re.fullmatch(r"08[3-9]\d{7}", d):
        d = "353" + d[1:]
    if re.fullmatch(r"447\d{9}", d) or re.fullmatch(r"3538[3-9]\d{7}", d):
        return d
    return ""


def _mobiles_in(text: str) -> Set[str]:
    return {n for n in (normalise_mobile(m) for m in _PHONE_RE.findall(text)) if n}


def _is_generic(email: str) -> bool:
    return email.split("@")[0].lower() in _GENERIC_LOCAL_PARTS


def _emails_in(text: str) -> Set[str]:
    return {e.lower().strip(".") for e in _EMAIL_RE.findall(text)}


def _name_from_email(email: str) -> Tuple[str, str]:
    """'nicola.jeffery@x' → ('Nicola', 'Jeffery'); anything less clear-cut → ('', '')."""
    parts = re.split(r"[._-]", email.split("@")[0])
    if len(parts) == 2 and all(p.isalpha() and len(p) > 1 for p in parts):
        return parts[0].capitalize(), parts[1].capitalize()
    return "", ""


def _split_name(name: str) -> Tuple[str, str]:
    words = [w for w in re.sub(r"[^\w\s'-]", " ", name or "").split() if w]
    if len(words) < 2:
        return "", ""
    return words[0], " ".join(words[1:])


def _same_person(from_name: str, from_email: str, contact: dict) -> bool:
    email = (contact.get("email") or "").lower()
    if not from_email or from_email == email:
        return True
    if email and from_email.split("@")[0] == email.split("@")[0]:
        return True   # same mailbox on another domain / alias
    name = (from_name or "").lower()
    first, last = (contact.get("first_name") or "").lower(), (contact.get("last_name") or "").lower()
    return bool(first and last and first in name and last in name)


# ── LLM extraction ────────────────────────────────────────────────────────────

_EXTRACT_TOOL = {
    "name": "extract_contacts",
    "description": "Extract contact details from an email reply to a job application.",
    "input_schema": {
        "type": "object",
        "properties": {
            "author_mobile": {
                "type": "string",
                "description": (
                    "The email AUTHOR's own mobile phone number, exactly as written — from "
                    "their signature ('Mobile:', 'M:', 'Cell:') or where they say to call "
                    "them on it. Omit if only an office/landline number is given, or the "
                    "number belongs to someone else."
                ),
            },
            "author_job_title": {
                "type": "string",
                "description": "The author's job title as written in their signature. Omit if not stated.",
            },
            "colleagues": {
                "type": "array",
                "description": (
                    "OTHER people (not the author, not Prateek) whom the email directs the "
                    "reader to contact instead or as an alternative — e.g. 'please contact "
                    "X in my absence', 'you should speak to X'. Do not include people merely "
                    "copied in or mentioned in passing, or shared mailboxes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Full name as written; omit if not given."},
                        "email": {"type": "string", "description": "Email address as written; omit if not given."},
                        "mobile": {"type": "string", "description": "Their mobile number as written; omit if not given."},
                        "job_title": {"type": "string", "description": "Their job title / role as written; omit if not given."},
                        "is_person": {
                            "type": "boolean",
                            "description": (
                                "true if this is one individual person; false if it is a shared or "
                                "departmental mailbox (sales, orders, support, service, IT, enquiries, "
                                "a team or a product line) — judge from the address and the context."
                            ),
                        },
                    },
                    "required": ["is_person"],
                },
            },
        },
    },
}


def _llm_extract(text: str, author: str) -> dict:
    try:
        return llm_client.complete_tool(
            "contact_extract",
            f"This email was written by {author or 'the contact'} in reply to a job "
            f"application from Prateek Sahni. Extract the contact details it contains: the "
            f"author's own mobile number, and any other people it tells the reader to "
            f"contact.\n\nEmail:\n{text}",
            _EXTRACT_TOOL,
            max_tokens=600,
        ) or {}
    except Exception as exc:
        log.warning("Contact extraction failed: %s", exc)
        return {}


def _verified_mobile(raw: str, found: Set[str]) -> str:
    n = normalise_mobile(raw or "")
    return n if n in found else ""


def _verified_title(raw: str, text: str) -> str:
    raw = (raw or "").strip()
    return raw if raw and raw.lower() in text.lower() else ""


# ── Public API ────────────────────────────────────────────────────────────────

def extract(messages: List[dict], contact: dict, known_contacts: Callable[[], Dict[str, str]]) -> dict:
    """
    messages: [{"from_name", "from_email", "body"}] — inbound messages for one contact
              (in-thread replies plus any separate auto-reply; an auto-reply found by
              searching on the contact's address has from_email "" = the contact).
    contact:  {"first_name", "last_name", "email", "mobile"} from the sheet row.
    known_contacts: returns {email: mobile} for every address already in any sender
                  tab (never re-added) — only called when a message involves an
                  address other than ours and the contact's.

    Returns {"mobile": the contact's new mobile or "",
             "other_mobiles": {email: mobile} for colleagues already in the sheet who wrote,
             "people": [new contact dicts], "notes": [str]}.
    """
    out = {"mobile": "", "other_mobiles": {}, "people": [], "notes": []}
    known = {}
    have_mobile = normalise_mobile(contact.get("mobile", ""))
    contact_email = (contact.get("email") or "").lower()
    seen = _OWN_EMAILS | {contact_email}
    loaded_known = False

    for msg in messages:
        from_email = (msg.get("from_email") or "").lower()
        if "mailer-daemon" in from_email or "postmaster" in from_email:
            continue
        text = own_text(msg.get("body", ""))
        if not text:
            continue

        by_contact = _same_person(msg.get("from_name", ""), from_email, contact)
        if by_contact and from_email:
            seen = seen | {from_email}     # their own alias in their signature isn't a new person
        mobiles = _mobiles_in(text)
        unfamiliar = {e for e in _emails_in(text) - seen if not _is_generic(e)}
        if not by_contact and from_email not in seen and not _is_generic(from_email):
            unfamiliar.add(from_email)
        if unfamiliar and not loaded_known:
            known = known_contacts()
            seen = seen | set(known)
            loaded_known = True
        new_emails = {e for e in _emails_in(text) - seen if not _is_generic(e)}
        new_author = (not by_contact and from_email not in seen and not _is_generic(from_email))

        # Regex gate: nothing that could change the sheet → no LLM call.
        if by_contact:
            author_has = have_mobile
        else:   # a colleague who already has their own row
            author_has = normalise_mobile(known.get(from_email, ""))
        new_mobile = mobiles - {author_has, out["mobile"], out["other_mobiles"].get(from_email)}
        if not (new_mobile or new_emails or new_author):
            continue

        author = msg.get("from_name") or from_email or contact.get("first_name", "")
        result = _llm_extract(text, author)

        author_mobile = _verified_mobile(result.get("author_mobile"), mobiles)
        if by_contact:
            if author_mobile and author_mobile != have_mobile:
                out["mobile"] = author_mobile
        elif from_email in known:
            if author_mobile and author_mobile != author_has:
                out["other_mobiles"][from_email] = author_mobile
        elif new_author:
            from_name = msg.get("from_name") or ""
            if "," in from_name:                          # "Gerry, William" → William Gerry
                last, first = [p.strip() for p in from_name.split(",", 1)]
            else:
                first, last = _split_name(from_name)
            if not first:
                first, last = _name_from_email(from_email)
            out["people"].append({
                "first_name": first, "last_name": last, "email": from_email,
                "mobile": author_mobile,
                "job_title": _verified_title(result.get("author_job_title"), text),
                "how": "replied on the contact's behalf",
                "in_thread": True,     # already in the conversation — not a cold-email target
            })
            seen.add(from_email)

        for c in result.get("colleagues") or []:
            email = (c.get("email") or "").lower().strip()
            mobile = _verified_mobile(c.get("mobile"), mobiles)
            first, last = _split_name(c.get("name", ""))
            if first and f"{first} {last}".lower() not in text.lower():
                first, last = "", ""                      # name not actually in the email
            if email:
                if email not in new_emails:               # invented, generic, or already known
                    continue
                is_person = c.get("is_person") is True
                if c.get("is_person") is False:
                    continue                              # shared mailbox, even if it has a label
                if not first and is_person:
                    first, last = _name_from_email(email)
                seen.add(email)
                new_emails.discard(email)
                if not first:
                    # No name to greet them by (phil@, j.feist@) — or a shared mailbox the
                    # model was unsure about. Leave it for a human rather than add a row.
                    if is_person:
                        out["notes"].append(f"alt contact: {email}")
                    continue
                out["people"].append({
                    "first_name": first, "last_name": last, "email": email,
                    "mobile": mobile, "job_title": _verified_title(c.get("job_title"), text),
                    "how": "named as an alternative contact",
                })
            elif first and (mobile or c.get("job_title")):
                # A name with no email can't become a row the pipeline can send to.
                bits = [f"{first} {last}", _verified_title(c.get("job_title"), text),
                        f"mobile {mobile}" if mobile else ""]
                out["notes"].append("alt contact: " + ", ".join(b for b in bits if b))

    return out


def has_new_mobile(body: str, have_mobile: str = "") -> bool:
    """Cheap regex check: does this message contain a UK/IE mobile other than the one on file?"""
    return bool(_mobiles_in(own_text(body)) - {normalise_mobile(have_mobile)})


def author_mobile(msg: dict, have_mobile: str) -> str:
    """The author's own mobile from this message, if it differs from the one on file."""
    text = own_text(msg.get("body", ""))
    have = normalise_mobile(have_mobile)
    found = _mobiles_in(text)
    if not (found - {have}):
        return ""
    result = _llm_extract(text, msg.get("from_name") or msg.get("from_email", ""))
    mobile = _verified_mobile(result.get("author_mobile"), found)
    return mobile if mobile != have else ""


# ── LinkedIn lookup ───────────────────────────────────────────────────────────

_COMPANY_NOISE = re.compile(r"\b(ltd|limited|plc|inc|llc|llp|group|uk|ireland|the|co)\b")
# Exa "people" highlight shape: "### Technical Solutions Manager - [JET PRESS Limited](https://www.linkedin.com/company/jetpresslimited) (Current)"
_CURRENT_ROLE_RE = re.compile(r"#+\s*([^#\n\[]+?)\s+-\s+\[([^\]]+)\]\(([^)]*)\)\s*\(Current\)")


def _norm_company(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _COMPANY_NOISE.sub(" ", (s or "").lower()))


def _company_matches(candidate: str, company: str, email_domain: str) -> bool:
    cand = _norm_company(candidate)
    if len(cand) < 3:
        return False
    targets = [_norm_company(company), re.sub(r"[^a-z0-9]", "", email_domain.split(".")[0].lower())]
    return any(t and len(t) >= 3 and (t in cand or cand in t) for t in targets)


def find_linkedin(first: str, last: str, company: str, email: str) -> Tuple[str, str]:
    """(profile URL, current job title) for a person, or ('', '') unless one result
    clearly matches both their full name and their company — a wrong profile is worse
    than a blank cell the user fills in by hand."""
    if not (_Exa and EXA_API_KEY and first and last):
        return "", ""
    domain = email.split("@")[-1] if "@" in email else ""
    try:
        results = _Exa(EXA_API_KEY).search(
            f"{first} {last} {company}",
            num_results=5,
            include_domains=["linkedin.com"],
            category="people",
            contents={"highlights": {"query": "current role and company", "maxCharacters": 400}},
        ).results
    except Exception as exc:
        log.warning("LinkedIn lookup failed for %s %s: %s", first, last, exc)
        return "", ""

    for r in results:
        url = getattr(r, "url", "") or ""
        title = (getattr(r, "title", "") or "").lower()
        if "/in/" not in url or not title.startswith(f"{first} {last}".lower()):
            continue
        text = " ".join(getattr(r, "highlights", None) or []) + " " + (getattr(r, "title", "") or "")
        role = ""
        for m in _CURRENT_ROLE_RE.finditer(text):
            if _company_matches(m.group(2), company, domain) or _company_matches(m.group(3), company, domain):
                role = m.group(1).strip()
                break
        else:
            # Title shape: "Stuart Jackman - Engineer at JW | LinkedIn"
            m = re.search(r" - (.+?) at (.+?)(?: \||$)", getattr(r, "title", "") or "")
            if not (m and _company_matches(m.group(2), company, domain)):
                continue
            role = m.group(1).strip()
        slug = url.rstrip("/").split("/in/")[-1]
        return f"https://www.linkedin.com/in/{slug}", role
    return "", ""
