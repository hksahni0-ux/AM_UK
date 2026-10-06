"""
AutoResponder: answers inbound replies to outreach threads automatically, for the
handful of reply types where the right answer is predictable — modelled on how
Prateek has been answering these by hand.

Reply types answered automatically (fixed templates, no free-form LLM writing):
  no_vacancy        "no roles right now / we'll keep your CV on file"
  forwarded         "I've passed your CV to HR / the MD / a colleague"
  acknowledged      "thanks, we'll review your CV and be in touch"
  apply_via_portal  "please apply through our careers page, we don't take CVs by email"
  role_coming_soon  "a relevant role will be advertised soon"
  referred          "you should contact <named person> instead"

Anything else is never answered automatically — the row is flagged
"needs your reply" in Comments instead (questions, call/interview requests,
sponsorship/visa, requests to stop emailing, mid-conversation messages, ...).

Guardrails, in the order they're applied:
  1. Only the newest message in a thread, only if it isn't ours, isn't an
     auto-responder/bounce/no-reply sender, and is between AUTO_REPLY_MIN_AGE_HOURS
     and AUTO_REPLY_MAX_AGE_DAYS old.
  2. Only the first message from that person in the thread — once a back-and-forth
     with someone has started, every later message from them is left to Prateek.
  3. One automatic reply per thread, ever (checked in the thread itself via the
     X-AMUK-Auto-Reply header, and in the auto_replies log tab).
  4. Hard "needs a human" triggers (questions, calls, sponsorship, AI remarks,
     "don't contact me") are checked with plain regexes BEFORE the LLM is asked —
     the LLM can never talk its way past them.
  5. The LLM's classification must be backed by that type's own keyword gate,
     otherwise it's downgraded to needs_human (see feedback on small-model
     classifiers over-firing — the keyword gate is what made left_company safe).
  6. Any name/role the template fills in must appear verbatim in their message.
  7. The finished reply is validated (greeting name, no leftover placeholders,
     length cap) and the per-account daily cap is enforced before sending.
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import List, Optional

import gspread
import pytz

from agents import llm_client
from agents.email_writer import _catchup_html, _catchup_plain, _sig_html, _sig_plain
from agents.gmail_agent import GmailAgent, _OOO_SUBJECT_PATTERNS
from agents.reply_classifier import has_departure_keywords
from agents.sheet_agent import SheetAgent, _load_creds
from config.settings import (
    AUTO_REPLY_MODE, AUTO_REPLY_DAILY_LIMIT, AUTO_REPLY_MIN_AGE_HOURS,
    AUTO_REPLY_MAX_AGE_DAYS, AUTO_REPLY_LOG_SHEET,
    SPREADSHEET_ID, SENDERS, TIMEZONE, CANDIDATE_WEBSITE,
    STATUS_DISCUSSION, STATUS_NO_ROLE, STATUS_BOUNCED,
    STATUS_NO_LONGER_WITH_COMPANY, REPLY_STATUS_RECEIVED,
)

log = logging.getLogger(__name__)

AUTO_REPLY_HEADER = "X-AMUK-Auto-Reply"

AUTO_TYPES = ("no_vacancy", "forwarded", "acknowledged", "apply_via_portal",
              "role_coming_soon", "referred")

_TYPE_LABELS = {
    "no_vacancy": "no vacancies, CV on file",
    "forwarded": "CV forwarded",
    "acknowledged": "acknowledged, will review",
    "apply_via_portal": "asked to apply via careers page",
    "role_coming_soon": "relevant role coming soon",
    "referred": "referred to another contact",
}

# ── Text cleanup ──────────────────────────────────────────────────────────────

_QUOTE_MARKERS = re.compile(
    r"^\s*(On .{0,200}wrote:|-{2,}\s*Original Message|From:\s|Sent:\s.*\d|_{10,}|>)",
    re.IGNORECASE | re.MULTILINE,
)
_URL_RE = re.compile(r"(https?://[^\s<>\]\)\"']+|www\.[^\s<>\]\)\"']+)", re.IGNORECASE)


# A whole line that is only a sign-off, optionally followed by a short name on the
# same line ("Cheers, Sam") — never a sentence like "Thank you for your email."
_SIGN_OFF_RE = re.compile(
    r"^((best|kind|warm|many)\s+)?(regards|wishes|thanks|thank you|cheers|sincerely|best|br)"
    r"[,!.]?(\s+([A-Z][A-Za-z'\-]+\.?)){0,2}\s*$",
    re.IGNORECASE,
)
_GENERIC_NAMES = {"hr", "team", "careers", "career", "recruitment", "recruiting", "info",
                  "information", "talent", "jobs", "admin", "hello", "enquiries", "office",
                  "people", "support", "contact", "sales", "mail"}
_TITLES = {"prof", "professor", "dr", "mr", "mrs", "ms", "miss", "sir", "dame", "eng"}


def _normalise_text(text: str) -> str:
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'") \
        .replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())


def new_text_only(body: str) -> str:
    """The part of a message they actually wrote this time — quoted history and their
    email signature cut off, so banner text in a signature ("Book a call with me",
    "Join our team!") can't be mistaken for what they said."""
    text = _normalise_text(body)
    m = _QUOTE_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    lines = [l for l in text.strip().splitlines()]
    content_seen = 0
    for i, line in enumerate(lines):
        if not line:
            continue
        if content_seen and len(line) <= 30 and _SIGN_OFF_RE.match(line):
            # keep the sign-off and the name line under it (if it looks like a name),
            # drop everything after
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            looks_like_name = (j < len(lines) and len(lines[j]) <= 40
                               and not _URL_RE.search(lines[j]) and not re.search(r"\d", lines[j]))
            lines = lines[:j + 1] if looks_like_name else lines[:i + 1]
            break
        content_seen += 1
    return "\n".join(lines).strip()


def _signoff_first_name(text: str) -> str:
    """The first name they signed with ('Kind regards\nTrish', 'Cheers, Sam'), or ''."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return ""
    candidates = []
    if _SIGN_OFF_RE.match(lines[-1]):
        candidates.append(re.sub(_SIGN_OFF_RE.pattern.split("[,!.]?")[0], "", lines[-1],
                                 flags=re.IGNORECASE).strip(" ,.!"))
    elif _SIGN_OFF_RE.match(lines[-2]):
        candidates.append(lines[-1])
    for cand in candidates:
        words = cand.split()
        if 1 <= len(words) <= 3 and all(w[:1].isupper() for w in words) \
                and not re.search(r"[\d@/|:]", cand):
            first = re.sub(r"[^A-Za-z'\-]", "", words[0])
            if len(first) >= 2 and first.lower() not in _GENERIC_NAMES | {"the"}:
                return first
    return ""


def _greeting_name(text: str, from_header: str) -> str:
    """Prefer the name they signed with ('Trish', 'Dave') when it's clearly the same
    person as the sender — a short form of their name, or part of their address —
    otherwise the From-header name. Stops 'Thanks, Rapid Fusion' → 'Hi Rapid'."""
    header = _first_name_from_header(from_header)
    signed = _signoff_first_name(text)
    if not signed or len(signed) < 3:
        return header
    local = parseaddr(from_header)[1].split("@")[0].lower()
    if header == "Team" or signed.lower() in local or header.lower()[:3] == signed.lower()[:3]:
        return signed
    return header


def _first_name_from_header(from_header: str) -> str:
    """'"Banks, John" <john.banks@x.com>' → 'John'; 'Prof David Curtis' → 'David';
    'HR - Careers <..>' / 'HMT Information <..>' → 'Team' (no person to greet)."""
    name, addr = parseaddr(from_header)
    name = name.strip().strip('"').strip()
    if "@" in name:                       # display name is itself an address
        name = re.split(r"[._]", name.split("@")[0])[0]
    if not name:
        return "Team"
    name = re.sub(r"\(.*?\)", " ", name)  # "Lisa O'Byrne (HR)" → "Lisa O'Byrne"
    if "," in name:                       # "Surname, First"
        name = name.split(",", 1)[1].strip()
    words = [re.sub(r"[^A-Za-z'\-]", "", w) for w in name.split()]
    words = [w for w in words if w and w.lower().rstrip(".") not in _TITLES]
    if not words or words[0].lower() in _GENERIC_NAMES or (words[0].isupper() and len(words[0]) <= 4):
        return "Team"
    first = words[0]
    return first[:1].upper() + first[1:]


# ── Hard "needs a human" triggers (checked before the LLM) ──────────────────

_HUMAN_TRIGGERS = [
    ("asks a question", re.compile(r"\?")),
    ("call / interview / meeting", re.compile(
        r"\b(interview|phone call|a call|quick call|teams call|zoom|teams meeting|"
        r"phone number|contact number|arrange a|set up a|schedule|availability|"
        r"are you free|meet (up|with you|for)|have a chat)\b", re.IGNORECASE)),
    ("sponsorship / right to work", re.compile(
        r"\b(visa|sponsor\w*|right to work|work permit|permit to work|"
        r"eligib\w+ to work|immigration)\b", re.IGNORECASE)),
    ("data-protection / GDPR", re.compile(r"\b(gdpr|data protection)\b", re.IGNORECASE)),
    ("formal application outcome", re.compile(
        r"(outcome of your application|did not progress|not been successful|"
        r"application (was|has been) unsuccessful|unsuccessful on this occasion)", re.IGNORECASE)),
    ("annoyed / irritated tone", re.compile(
        r"(you are not getting|you're not getting|not sure how you (got|obtained)|"
        r"how did you get my|i will be direct|please stop)", re.IGNORECASE)),
    ("salary / notice / start date", re.compile(
        r"\b(salary|notice period|start date|expected (pay|rate)|day rate)\b", re.IGNORECASE)),
    ("asked not to be contacted", re.compile(
        r"(do not (contact|email)|don't (contact|email)|stop (contacting|emailing|sending)|"
        r"unsubscribe|remove me|no (need|requirement) (for you )?to follow up|"
        r"not appropriate|please don't|several (e-?mails|times)|two more occasions)",
        re.IGNORECASE)),
    ("comment about AI-written email", re.compile(
        r"\b(use ai|using ai|ai to write|written by ai|chatgpt)\b", re.IGNORECASE)),
]


_STRONG = ("asked not to be contacted", "comment about AI-written email", "annoyed / irritated tone")


def _strong_trigger(full_text: str) -> str:
    """The triggers worth checking below the sign-off too (a 'PS — don't use AI' line)."""
    scrubbed = _URL_RE.sub(" ", full_text)
    for reason, rx in _HUMAN_TRIGGERS:
        if reason in _STRONG and rx.search(scrubbed):
            return reason
    return ""


def human_trigger(text: str) -> str:
    """Returns the reason a human must answer this, or '' if none fired."""
    scrubbed = _URL_RE.sub(" ", text)  # '?' inside URLs isn't a question
    for reason, rx in _HUMAN_TRIGGERS:
        if rx.search(scrubbed):
            return reason
    return ""


# ── Per-type keyword gates (the LLM's verdict must be backed by one of these) ─

_GATES = {
    "no_vacancy": re.compile(
        r"(no (current |open |suitable |available )?(vacanc|position|role|opening|opportunit|requirement)|"
        r"not (currently |actively )?(recruiting|hiring|looking to (hire|recruit))|"
        r"(don't|do not|doesn't|does not) (currently )?have any|no open|not in a position to offer|"
        r"filled all|we have filled|"
        r"(keep|hold|retain|hold on to) (your|the) (cv|resume|resumé|details|profile|application|files?)|"
        r"on (file|record)|been filled|filled (internally|this)|not something we wish to progress|"
        r"aren't hiring|are not hiring|unable to offer)", re.IGNORECASE),
    "forwarded": re.compile(
        r"(forward|passed (it|this|your|on)|pass(ed)? (it|this|your \w+) (on|to)|"
        r"shared your|copied|cc'?d|cc'?ing|sent (it|this|your \w+) (on|to)|"
        r"passed your details)", re.IGNORECASE),
    "acknowledged": re.compile(
        r"(will (review|look|consider|come back|get back|respond|be in touch)|"
        r"(review|reviewed) your (cv|resume|application|profile)|respond in due course|"
        r"been received|have received|received your)", re.IGNORECASE),
    "apply_via_portal": re.compile(
        r"(career|careers|vacanc\w* page|jobs? (site|page|board|portal)|portal|"
        r"apply (directly|online|through|via|on)|application (portal|process|system)|"
        r"recruitment (site|system|management)|website|linkedin|indeed|"
        r"(accept|take|process) applications|official (way|route|channel)|"
        r"(open|current) (positions|vacancies|roles|opportunities) (here|at|on|are)|"
        r"list of (our )?(open )?(positions|vacancies|roles))", re.IGNORECASE),
    "role_coming_soon": re.compile(
        r"(coming (weeks|months)|next (few|couple of) (weeks|months)|upcoming|"
        r"(will|expect to|plan to|going to) (be )?(advertis|post|release|open|go live|recruit)|"
        r"going live|later this (month|year))", re.IGNORECASE),
    "referred": re.compile(
        r"(best person|right person|contact|reach out to|speak (to|with)|get in touch with|"
        r"would be (best|better) placed|sits with|responsible for)", re.IGNORECASE),
}

_COMPANY_WORDS = re.compile(
    r"\b(ltd|limited|group|consult\w*|recruit\w*|agency|team|department|services|"
    r"solutions|plc|inc|llp|partners|hr)\b", re.IGNORECASE)

# ── LLM routing ───────────────────────────────────────────────────────────────

_ROUTE_TOOL = {
    "name": "route_reply",
    "description": "Decide what kind of response an inbound reply to a job-outreach email is.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply_type": {
                "type": "string",
                "enum": list(AUTO_TYPES) + ["needs_human", "no_reply_needed"],
                "description": (
                    "'no_vacancy' — they have no suitable role / aren't hiring right now (often "
                    "'we'll keep your CV on file'). "
                    "'forwarded' — they have passed the CV/email on to HR, a manager or a colleague "
                    "and that's the main point. "
                    "'acknowledged' — they received it and will review/respond, nothing else. "
                    "'apply_via_portal' — they ask him to apply through the careers page/portal/"
                    "LinkedIn, or say they can't accept applications by email. "
                    "'role_coming_soon' — they say a relevant role will be advertised/opened soon. "
                    "'referred' — they say a specific named other person is the one to contact. "
                    "'needs_human' — anything that asks him something, needs a decision, raises "
                    "sponsorship/visa/salary, is hostile, mixes several of the above in a way a "
                    "short standard reply would mishandle, or you are not sure. "
                    "'no_reply_needed' — a closing pleasantry ('good luck', 'will do', 'thanks') "
                    "or an automated message that needs no answer."
                ),
            },
            "referred_name": {
                "type": "string",
                "description": "Only for 'referred': the full name of the person they point him to, "
                               "exactly as written in the email. Omit otherwise.",
            },
            "upcoming_role": {
                "type": "string",
                "description": "Only for 'role_coming_soon': the role title exactly as written in "
                               "the email, if one is named. Omit otherwise.",
            },
            "reason": {"type": "string", "description": "One short sentence explaining the choice."},
        },
        "required": ["reply_type", "reason"],
    },
}


def route_with_llm(text: str, company: str) -> dict:
    prompt = (
        "Prateek Sahni sent a speculative job-application email to someone at "
        f"{company or 'a company'}. Below is the new part of their reply (quoted history "
        "removed). Classify it so the right standard response can be chosen. When in "
        "doubt, choose 'needs_human' — a wrong automatic reply to a recruiter is far "
        "worse than a missed one.\n\n"
        f"REPLY:\n{text[:2500]}"
    )
    return llm_client.complete_tool("reply_route", prompt, _ROUTE_TOOL, max_tokens=300)


# ── Templates (Prateek's own phrasing, lightly polished) ─────────────────────

def _short_company(company: str) -> str:
    """'Laser Lines (Stratasys UK Platinum Partner & ...)' → 'Laser Lines';
    'OGM: Plastic Injection Moulder' → 'OGM' — the name as it reads in a sentence."""
    name = re.sub(r"\s*\(.*$", "", company or "")
    name = re.split(r"\s*[:|–—]\s*|\s+-\s+", name)[0]
    return re.sub(r"\s+", " ", name).strip() or "the company"


def _paragraphs(reply_type: str, first: str, company: str, extras: dict) -> List[str]:
    company = _short_company(company)
    if reply_type == "no_vacancy":
        return [
            f"Hi {first},",
            "Thanks for getting back to me, and for the update.",
            "Please do keep my profile on file for future opportunities, as I'm actively "
            "looking for a role. I'd be glad to hear from you should anything suitable come up.",
        ]
    if reply_type == "forwarded":
        return [
            f"Hi {first},",
            "Thank you for forwarding my CV to the team. I really appreciate your help.",
            "I look forward to hearing from them.",
        ]
    if reply_type == "acknowledged":
        return [
            f"Hi {first},",
            "Thank you for the acknowledgement.",
            f"I look forward to hearing from you and the team, and to discussing how my "
            f"experience could support {company}.",
        ]
    if reply_type == "apply_via_portal":
        # No direct "please forward my CV" ask: they've just said the portal is the way in,
        # and pushing past that reads as not listening — in Prateek's own threads it drew
        # irritation or silence, while effort + a soft "keep me in mind" landed better.
        role = extras.get("role_applied", "")
        apply_line = (f"I'll apply for the {role} role through {company}'s careers page."
                      if role else f"I'll apply through {company}'s careers page for any suitable roles.")
        return [
            f"Hi {first},",
            f"Thank you for getting back to me, and for pointing me to the right channel. {apply_line}",
            "As I'm applying from overseas, I also make a point of researching the companies I'd "
            "genuinely like to join and introducing myself to the people there directly, rather than "
            f"relying on an online form alone. {company} is one of the companies I've specifically "
            "chosen to approach this way.",
            "If a role comes up that you think would suit my background, I'd be grateful if you'd "
            f"keep me in mind. My portfolio at {CANDIDATE_WEBSITE} also gives a quick overview of the "
            "projects I've delivered.",
            "Thank you again for your time.",
        ]
    if reply_type == "role_coming_soon":
        role = extras.get("upcoming_role", "")
        what = f"the upcoming {role} opportunity" if role else "the upcoming opportunity"
        return [
            f"Hi {first},",
            f"Thank you for letting me know about {what}.",
            "I'll keep a close eye on your careers page and LinkedIn and apply as soon as it "
            "goes live. In the meantime, I'd really appreciate it if you could keep my profile "
            "in mind for it.",
        ]
    if reply_type == "referred":
        who = extras["referred_name"]
        return [
            f"Hi {first},",
            f"Thank you for pointing me towards {who}. I really appreciate it.",
            f"Would you be happy to pass my CV on to {who}, or connect us by email? I'd welcome "
            "the chance to discuss how my experience could support the team.",
        ]
    raise ValueError(f"No template for {reply_type}")


def render_reply(reply_type: str, first: str, company: str, sender_email: str,
                 extras: Optional[dict] = None) -> (str, str):
    paras = _paragraphs(reply_type, first, company, extras or {})
    plain = "\n\n".join(paras) + f"\n\n{_catchup_plain(0)}\n\n{_sig_plain(sender_email, 0)}"
    html_paras = "".join(f'<p style="margin:0 0 12px 0;">{p}</p>' for p in paras)
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;font-size:14px;color:#000000;'
        'line-height:1.6;max-width:700px;">'
        f"{html_paras}{_catchup_html(0)}"
        f'<p style="margin:0 0 12px 0;">{_sig_html(sender_email, 0)}</p></body></html>'
    )
    return plain, html


def validate_reply(plain: str, first: str) -> str:
    """Returns a problem description, or '' if the reply is safe to send."""
    if not first or len(first) < 2:
        return "no usable first name to greet"
    if not plain.startswith(f"Hi {first},"):
        return "greeting doesn't match their name"
    if re.search(r"[{}\[\]<>]|\bNone\b|\bTODO\b", plain.split("\n---\n")[0]):
        return "leftover placeholder in reply body"
    if len(plain.split("\n---\n")[0]) > 1400:
        return "reply body too long"
    return ""


# ── Decision ──────────────────────────────────────────────────────────────────

@dataclass
class Decision:
    action: str                  # "reply" | "flag" | "skip"
    reason: str
    reply_type: str = ""
    to: str = ""
    first_name: str = ""
    plain: str = ""
    html: str = ""
    extras: dict = field(default_factory=dict)


def _is_automated(msg: dict) -> bool:
    h = msg["headers"]
    frm = msg["from"].lower()
    if any(k in frm for k in ("mailer-daemon", "postmaster", "no-reply", "noreply", "donotreply",
                              "do-not-reply", "notifications@")):
        return True
    if h.get("auto-submitted", "no").lower() not in ("", "no"):
        return True
    if h.get("x-autoreply") or h.get("x-autorespond"):
        return True
    if h.get("precedence", "").lower() in ("bulk", "auto_reply", "junk", "list"):
        return True
    subject = h.get("subject", "").lower()
    return any(p in subject for p in _OOO_SUBJECT_PATTERNS)


def decide(messages: List[dict], own_email: str, company: str, now: datetime,
           use_llm=route_with_llm, check_age: bool = True, role_applied: str = "") -> Decision:
    """
    messages: the thread, oldest first, each {"from","headers","body","date","self"}.
    Pure apart from the LLM call (injectable for the backtest).
    """
    if not messages:
        return Decision("skip", "empty thread")
    latest = messages[-1]
    if latest["self"]:
        return Decision("skip", "last message is ours")
    if any(m["self"] and m["headers"].get(AUTO_REPLY_HEADER.lower()) for m in messages):
        return Decision("skip", "thread already had an automatic reply")
    if _is_automated(latest):
        return Decision("skip", "automated / out-of-office / no-reply sender")

    if check_age:
        age = now - latest["date"]
        if age < timedelta(hours=AUTO_REPLY_MIN_AGE_HOURS):
            return Decision("skip", f"too recent ({age.total_seconds() / 3600:.1f}h old)")
        if age > timedelta(days=AUTO_REPLY_MAX_AGE_DAYS):
            return Decision("skip", f"too old ({age.days}d)")

    sender_addr = parseaddr(latest["from"])[1].lower()
    ongoing = any(not m["self"] and parseaddr(m["from"])[1].lower() == sender_addr
                  for m in messages[:-1])

    text = new_text_only(latest["body"])
    if not text:
        return Decision("flag", "couldn't read their message")
    trigger = human_trigger(text) or _strong_trigger(_normalise_text(
        _QUOTE_MARKERS.split(_normalise_text(latest["body"]))[0]))
    if trigger:
        return Decision("flag", trigger)
    if has_departure_keywords(text):
        # the contact has left — reply_checker records that; a new lead may be named
        return Decision("flag", "says the contact has left the company")

    try:
        routed = use_llm(text, company)
    except Exception as exc:
        log.warning("AutoResponder: routing failed (%s) — leaving for a human", exc)
        return Decision("flag", "couldn't classify their message")

    reply_type = routed.get("reply_type", "needs_human")
    reason = routed.get("reason", "")
    if reply_type == "no_reply_needed":
        return Decision("skip", f"no reply needed — {reason}")
    if ongoing:
        # a back-and-forth with this person has already started — always Prateek's call
        return Decision("flag", "ongoing conversation with this person")
    if reply_type not in AUTO_TYPES:
        return Decision("flag", reason or "needs a personal reply")
    if not _GATES[reply_type].search(text):
        return Decision("flag", f"classified as {reply_type} but wording doesn't confirm it")

    extras = {}
    if reply_type == "referred" and _GATES["forwarded"].search(text):
        # "this sits with Claire — I've forwarded it to her": already done, just thank them
        reply_type = "forwarded"
    if reply_type == "referred":
        who = (routed.get("referred_name") or "").strip()
        words = who.split()
        if (not who or who not in text or not 2 <= len(words) <= 3
                or any(not w[:1].isupper() or any(c.isdigit() for c in w) for w in words)
                or _COMPANY_WORDS.search(who)):
            return Decision("flag", "referred elsewhere, but not to a named person")
        extras["referred_name"] = who
    if reply_type == "role_coming_soon":
        role = (routed.get("upcoming_role") or "").strip()
        if role and role in text and len(role) <= 60:
            extras["upcoming_role"] = role
    if reply_type == "apply_via_portal":
        urls = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
        if urls:
            extras["portal_url"] = urls[0]
        if role_applied and role_applied.lower() != "open application" and len(role_applied) <= 70:
            extras["role_applied"] = re.sub(r"\s*\(.*?\)", "", role_applied).strip()

    first = _greeting_name(text, latest["from"])
    plain, html = render_reply(reply_type, first, company, own_email, extras)
    problem = validate_reply(plain, first)
    if problem:
        return Decision("flag", f"{_TYPE_LABELS[reply_type]} — but {problem}")

    return Decision("reply", reason, reply_type=reply_type, to=latest["from"],
                    first_name=first, plain=plain, html=html, extras=extras)


# ── Gmail / sheet plumbing ────────────────────────────────────────────────────

def _normalise(msg: dict, gmail: GmailAgent, own_email: str) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    date = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return {
        "id": msg["id"],
        "from": headers.get("from", ""),
        "headers": headers,
        "body": gmail._extract_body_text(msg),
        "date": date,
        "self": own_email in headers.get("from", "").lower(),
        "thread_id": msg.get("threadId", ""),
    }


class _ReplyLog:
    """The auto_replies tab: an audit trail, the one-per-thread record and the daily-cap counter."""
    HEADER = ["Date", "Time", "Account", "Thread ID", "To", "Company", "Type", "Mode", "Detail"]

    def __init__(self, read_only: bool = False):
        self.read_only = read_only
        gc = gspread.authorize(_load_creds(SENDERS[0]["token_file"]))
        gc.set_timeout(30)
        book = gc.open_by_key(SPREADSHEET_ID)
        try:
            self.ws = book.worksheet(AUTO_REPLY_LOG_SHEET)
            self.rows = self.ws.get_all_values()[1:]
        except gspread.WorksheetNotFound:
            self.ws, self.rows = None, []
            if not read_only:
                self.ws = book.add_worksheet(AUTO_REPLY_LOG_SHEET, rows=1000, cols=len(self.HEADER))
                self.ws.append_row(self.HEADER)

    def replied_threads(self) -> set:
        return {r[3] for r in self.rows if len(r) > 7 and r[7] == "send"}

    def sent_today(self, account: str, today_str: str) -> int:
        return sum(1 for r in self.rows if len(r) > 7 and r[0] == today_str
                   and r[2] == account and r[7] == "send")

    def add(self, row: list):
        if not self.read_only:
            self.ws.append_row(row, value_input_option="RAW")
        self.rows.append(row)


def _pick_row(rows: List[dict], sender_addr: str) -> Optional[dict]:
    for r in rows:
        if r["email"].lower() == sender_addr:
            return r
    for r in rows:
        if r["seq"] > 0:
            return r
    return rows[0] if rows else None


def _comment(existing: str, note: str) -> str:
    if note in existing:
        return existing
    return f"{note} | {existing}" if existing else note


def run_for_sender(sender: dict, reply_log: _ReplyLog, mode: str = AUTO_REPLY_MODE):
    """mode: "send" | "dry_run" (log decisions only — no emails, no sheet writes) | "off"."""
    name = sender["email"]
    if mode == "off":
        return
    dry = mode != "send"
    gmail = GmailAgent(sender)
    sheet = SheetAgent(sender)
    own = sender["email"].lower()
    now = datetime.now(timezone.utc)
    uk_now = datetime.now(pytz.timezone(TIMEZONE))
    today_str = uk_now.strftime("%d %b %Y")

    # Map thread ID → sheet rows
    by_thread = {}
    for item in sheet._all_rows():
        d = item["data"]
        tid = sheet._get_val(d, "thread_id")
        if not tid:
            continue
        try:
            seq = int(sheet._get_val(d, "sequence_step") or 0)
        except ValueError:
            seq = 0
        by_thread.setdefault(tid, []).append({
            "row_number": item["row_number"],
            "email": sheet._get_val(d, "recipient_email"),
            "company": sheet._get_val(d, "company_name"),
            "status": sheet._get_val(d, "status"),
            "comments": sheet._get_val(d, "comments"),
            "role_applied": sheet._get_val(d, "role_applied"),
            "seq": seq,
        })

    listed = gmail.service.users().messages().list(
        userId="me", q=f"newer_than:{AUTO_REPLY_MAX_AGE_DAYS + 1}d -from:me -in:chats",
        maxResults=100,
    ).execute(num_retries=5).get("messages", [])
    thread_ids = sorted({m["threadId"] for m in listed if m["threadId"] in by_thread})
    already = reply_log.replied_threads()

    for tid in thread_ids:
        rows = by_thread[tid]
        if any(r["status"] in (STATUS_BOUNCED, STATUS_NO_LONGER_WITH_COMPANY) for r in rows):
            continue
        try:
            thread = gmail.service.users().threads().get(
                userId="me", id=tid, format="full").execute(num_retries=5)
            messages = [_normalise(m, gmail, own) for m in thread.get("messages", [])]
            company = rows[0]["company"]
            role = next((r["role_applied"] for r in rows if r["role_applied"]), "")
            decision = decide(messages, own, company, now, role_applied=role)
            if tid in already and decision.action == "reply":
                decision = Decision("skip", "thread already in auto_replies log")
            if decision.action == "skip":
                log.debug("[%s] thread %s: skip — %s", name, tid, decision.reason)
                continue

            latest = messages[-1]
            sender_addr = parseaddr(latest["from"])[1].lower()
            row = _pick_row(rows, sender_addr)
            live_row = sheet._live_row_number(row["email"], row["row_number"])

            if decision.action == "flag" and dry:
                log.info("[%s] DRY RUN — would flag %s: %s", name, sender_addr, decision.reason)
                continue
            if decision.action == "flag":
                note = f"needs your reply ({decision.reason}) — {parseaddr(latest['from'])[0] or sender_addr}"
                if note in row["comments"]:
                    continue  # already flagged on an earlier cycle
                updates = {"reply_status": REPLY_STATUS_RECEIVED,
                           "comments": _comment(row["comments"], note)}
                if row["status"] not in (STATUS_NO_ROLE,):
                    updates["status"] = STATUS_DISCUSSION
                sheet._write_updates(live_row, updates)
                log.info("[%s] %s: flagged for manual reply — %s", name, sender_addr, decision.reason)
                continue

            # decision.action == "reply"
            if reply_log.sent_today(name, today_str) >= AUTO_REPLY_DAILY_LIMIT:
                log.info("[%s] Auto-reply daily limit reached — leaving %s for tomorrow", name, sender_addr)
                return
            detail = decision.extras.get("portal_url") or decision.extras.get("referred_name") \
                or decision.extras.get("upcoming_role") or ""
            if dry:
                log.info("[%s] DRY RUN — would auto-reply (%s) to %s:\n%s", name,
                         decision.reply_type, sender_addr, decision.plain.split("\n---\n")[0])
                continue
            if mode == "send":
                _send(gmail, sender, tid, latest, messages, decision)
                status = STATUS_NO_ROLE if decision.reply_type == "no_vacancy" else STATUS_DISCUSSION
                note = f"auto-replied {today_str} ({_TYPE_LABELS[decision.reply_type]})"
                if detail:
                    note += f": {detail}"
                sheet._write_updates(live_row, {
                    "status": status,
                    "reply_status": REPLY_STATUS_RECEIVED,
                    "comments": _comment(row["comments"], note),
                })
            reply_log.add([today_str, uk_now.strftime("%H:%M"), name, tid, sender_addr, company,
                           decision.reply_type, mode, detail])
            log.info("[%s] Auto-reply (%s) sent to %s — %s", name, decision.reply_type,
                     sender_addr, company)
        except Exception as exc:
            log.error("[%s] Auto-reply failed for thread %s: %s", name, tid, exc)


def _send(gmail: GmailAgent, sender: dict, thread_id: str, latest: dict, messages: List[dict],
          decision: Decision):
    h = latest["headers"]
    subject = h.get("subject", "") or next((m["headers"].get("subject", "") for m in messages), "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg_id = h.get("message-id", "")
    refs = (h.get("references", "") + " " + msg_id).strip()

    mime = MIMEMultipart("alternative")
    mime["From"] = f"{sender['name']} <{sender['email']}>"
    mime["To"] = decision.to
    mime["Subject"] = subject
    if msg_id:
        mime["In-Reply-To"] = msg_id
        mime["References"] = refs
    mime[AUTO_REPLY_HEADER] = decision.reply_type
    mime.attach(MIMEText(decision.plain, "plain", "utf-8"))
    mime.attach(MIMEText(decision.html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    gmail.service.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id},
    ).execute(num_retries=5)


def run_all(mode: str = AUTO_REPLY_MODE):
    if mode == "off":
        log.info("Auto-replies are off")
        return
    reply_log = _ReplyLog(read_only=(mode != "send"))
    for sender in SENDERS:  # sequential — the shared log tab is the daily-cap/dedup source of truth
        try:
            run_for_sender(sender, reply_log, mode)
        except Exception as exc:
            log.error("[%s] Auto-responder failed: %s", sender["email"], exc)
