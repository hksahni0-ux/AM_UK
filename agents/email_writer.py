"""
EmailWriterAgent: generates HTML outreach emails matching Prateek's established format.

Bold usage (Claude outputs **text**, converted to <b> for HTML):
- Role title being applied for
- Company names (first mention in each paragraph)
- Role + company when describing own experience ("as a **Senior Process Engineer at Precise Axis**")
- Key technical tools/skills when listed

Signature: name in bold, LinkedIn clickable, phone plain text.
Sends multipart/alternative (HTML + plain text fallback).
"""

import logging
import re
from functools import lru_cache

import pdfplumber
try:
    from exa_py import Exa as _Exa
except ImportError:
    _Exa = None

from agents import llm_client
from config.settings import (
    EXA_API_KEY, CV_CONTEXT, DEFAULT_COUNTRY,
    CANDIDATE_NAME, CANDIDATE_PHONE, CANDIDATE_LINKEDIN,
    get_country_config,
)

log = logging.getLogger(__name__)

# ── Conversion helpers ────────────────────────────────────────────────────────

_CALENDLY_URL = "https://calendly.com/prateeksahni/catchup"

# Gmail collapses trailing content under "Show quoted text" when it's byte-identical
# to what already appeared earlier in the same thread. Follow-ups (seq 1/2) send as
# replies in the initial email's thread, so the sign-off word still varies by
# sequence_step to keep the whole message visible without a click; the catchup
# line itself stays constant across all mails per requirement.
_CATCHUP_LINE = "Happy to answer any questions — a link to my calendar is below if a quick call works better:"

_SIGN_OFFS = ["Kind regards,", "Best regards,", "Warm regards,"]


def _catchup_html(sequence_step: int) -> str:
    return (
        '<hr style="border:none;border-top:1px solid #cccccc;margin:0 0 12px 0;">'
        f'<p style="margin:0 0 12px 0;">{_CATCHUP_LINE}<br><br>'
        f'<a href="{_CALENDLY_URL}" style="color:#1155CC;text-decoration:none;">{_CALENDLY_URL}</a>'
        '</p>'
    )


def _catchup_plain(sequence_step: int) -> str:
    return f"---\n\n{_CATCHUP_LINE}\n\n{_CALENDLY_URL}"


def _to_html(text: str, sig_html: str, sequence_step: int) -> str:
    """Convert Claude's plain text + **bold** markers to a clean HTML email body."""
    # Convert **bold** to <b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Split on blank lines → paragraphs
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    html_paras = []
    for p in paragraphs:
        p = p.replace('\n', '<br>')
        html_paras.append(f'<p style="margin:0 0 12px 0;">{p}</p>')
    body_html = '\n'.join(html_paras)

    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,sans-serif;font-size:14px;'
        'color:#000000;line-height:1.6;max-width:700px;">'
        f'{body_html}'
        f'{_catchup_html(sequence_step)}'
        f'<p style="margin:0 0 12px 0;">{sig_html}</p>'
        '</body></html>'
    )


def _to_plain(text: str, sig_plain: str, sequence_step: int) -> str:
    """Strip **bold** markers for the plain text fallback."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    return f"{text.strip()}\n\n{_catchup_plain(sequence_step)}\n\n{sig_plain}"


def _sig_html(sender_email: str, sequence_step: int) -> str:
    linkedin_url = CANDIDATE_LINKEDIN if CANDIDATE_LINKEDIN.startswith("http") \
        else f"https://{CANDIDATE_LINKEDIN}"
    phone_digits = CANDIDATE_PHONE.replace(" ", "")
    link_style = "color:#1155CC;text-decoration:none;"
    sign_off = _SIGN_OFFS[min(sequence_step, len(_SIGN_OFFS) - 1)]
    return (
        f"{sign_off}<br>"
        f"<b>{CANDIDATE_NAME}</b><br>"
        f'<a href="mailto:{sender_email}" style="{link_style}">{sender_email}</a>'
        f" | "
        f'<a href="tel:{phone_digits}" style="{link_style}">{CANDIDATE_PHONE}</a><br>'
        f'<a href="{linkedin_url}" style="{link_style}">{CANDIDATE_LINKEDIN}</a>'
    )


def _sig_plain(sender_email: str, sequence_step: int) -> str:
    sign_off = _SIGN_OFFS[min(sequence_step, len(_SIGN_OFFS) - 1)]
    return (
        f"{sign_off}\n"
        f"{CANDIDATE_NAME}\n"
        f"{sender_email} | {CANDIDATE_PHONE}\n"
        f"{CANDIDATE_LINKEDIN}"
    )


# ── CV extraction ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _extract_cv_text(cv_path: str) -> str:
    try:
        parts = []
        with pdfplumber.open(cv_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
        full = "\n".join(parts)
        return full[:4000] if len(full) > 4000 else full
    except Exception as exc:
        log.error("CV extraction failed for %s: %s", cv_path, exc)
        return CV_CONTEXT


def _fetch_linkedin_profile(linkedin_url: str, first_name: str, last_name: str, company: str) -> str:
    """Search Exa for the contact's LinkedIn profile and return a usable snippet."""
    if not linkedin_url or not _Exa or not EXA_API_KEY:
        return ""
    try:
        exa = _Exa(EXA_API_KEY)
        # Extract vanity slug from URL — more reliable than contact name for matching
        vanity = linkedin_url.rstrip("/").split("/in/")[-1] if "/in/" in linkedin_url else ""
        # Build query: prefer vanity-slug-based name (hyphens → spaces) + company
        if vanity:
            # Convert "benjamin-rombough-91161829" → "benjamin rombough" (drop numeric suffix)
            parts = vanity.replace("-", " ").split()
            slug_name = " ".join(p for p in parts if not p.isdigit())
            query = f"{slug_name} {company}"
        else:
            query = f"{first_name} {last_name} {company}".strip()

        results = exa.search(
            query,
            num_results=3,
            include_domains=["linkedin.com"],
            category="people",
            contents={"highlights": {"query": "professional background experience", "maxCharacters": 600}},
        )
        for r in results.results:
            url = getattr(r, "url", "") or ""
            # Accept if URL contains the vanity slug OR the query matches well
            if vanity and vanity not in url and vanity.rsplit("-", 1)[0] not in url:
                continue
            snippets = []
            if hasattr(r, "highlights") and r.highlights:
                snippets.extend(r.highlights[:2])
            elif hasattr(r, "text") and r.text:
                snippets.append(r.text[:500])
            if snippets:
                return "\n".join(snippets)[:700].strip()
        return ""
    except Exception as exc:
        log.debug("Exa LinkedIn fetch failed: %s", exc)
        return ""


def _ask_claude(prompt: str) -> str:
    return llm_client.complete("email_write", prompt, max_tokens=1500)


# ── Public API ────────────────────────────────────────────────────────────────

def write_email(
    row: dict,
    job: dict,
    sequence_step: int,
    sender_name: str,
    sender_email: str,
    cv_path: str,
) -> dict:
    """Returns {"subject": str, "body_html": str, "body_plain": str}."""
    cv_text = _extract_cv_text(cv_path)
    sh = _sig_html(sender_email, sequence_step)
    sp = _sig_plain(sender_email, sequence_step)

    first_name = row.get("first_name", "").strip()
    company = row.get("company_name", "your company")
    industry = row.get("company_industry", "")
    contact_title = row.get("recipient_job_title", "")
    role_title = job.get("role_title", "Open Application")
    role_desc = job.get("role_description", "")
    is_open = job.get("is_open_application", True)
    linkedin_url = row.get("person_linkedin_url", "").strip()
    last_name = row.get("last_name", "").strip()
    country = row.get("country") or DEFAULT_COUNTRY

    greeting = f"Hi {first_name}," if first_name else "Hi there,"

    # Fetch live LinkedIn profile data for initial emails only
    linkedin_snippet = ""
    if sequence_step == 0 and linkedin_url:
        linkedin_snippet = _fetch_linkedin_profile(linkedin_url, first_name, last_name, company)

    if sequence_step == 0:
        raw, subject = _write_initial(
            cv_text, greeting, company, industry, contact_title,
            role_title, role_desc, is_open,
            job.get("company_mission", ""),
            job.get("company_focus", ""),
            job.get("company_vision", ""),
            job.get("company_notable", ""),
            linkedin_url,
            linkedin_snippet,
            country,
        )
    elif sequence_step == 1:
        raw, subject = _write_followup_1(cv_text, greeting, company, industry, role_title, is_open, contact_title)
    else:
        raw, subject = _write_followup_2(cv_text, greeting, company, role_title, is_open, contact_title)

    raw = _inject_warm_opener(raw)

    return {
        "subject": subject,
        "body_html": _to_html(raw, sh, sequence_step),
        "body_plain": _to_plain(raw, sp, sequence_step),
    }


# ── Email generators ──────────────────────────────────────────────────────────

_BOLD_RULES = """
Bold formatting rules — use **double asterisks** around:
- The specific role title being applied for (e.g. **Lead Manufacturing Engineer**)
- Company name on first mention per paragraph (e.g. **TISICS**)
- Role + company when describing own experience (e.g. **Senior Process Engineer at Precise Axis**)
- Lists of key technical tools (e.g. **SolidWorks, NX, and Ansys**)
Do NOT bold: metrics, dates, generic phrases, the opening greeting, or the closing line.
"""

_STYLE_RULES = """
Strict style rules:
- Formal British English throughout
- No markdown except **bold** markers as instructed above
- No bullet points, no dashes for emphasis, no em dashes
- Every factual claim must come directly from the CV provided
- Do NOT include the sign-off — it will be appended automatically
- Do NOT write "Please find my CV attached" in the closing — it will be appended automatically
- WORD COUNT: the entire body excluding the greeting line must be 200–240 words. Every sentence must earn its place — cut padding and repetition ruthlessly.
- Keep each sentence under 30 words. Split long sentences rather than losing meaning.
"""


_OPENER_WORDS = [
    "resonated with me",
    "immediately stood out",
    "drew my attention",
    "struck a chord with my background",
    "aligned closely with my own experience",
    "caught my eye",
    "piqued my interest",
    "felt like a natural fit",
]

_HR_KEYWORDS = frozenset({
    "hr", "human resources", "recruiter", "recruiting", "recruitment",
    "talent acquisition", "talent", "people & culture", "people and culture",
    "hiring", "people ops", "people operations",
})


def _is_hr_recipient(contact_title: str) -> bool:
    title_lower = contact_title.lower()
    return any(kw in title_lower for kw in _HR_KEYWORDS)


def _inject_warm_opener(text: str) -> str:
    """Insert 'I hope this email finds you well.' after the greeting line."""
    idx = text.find('\n\n')
    if idx == -1:
        return text
    return text[:idx] + '\n\nI hope my email finds you well.\n\n' + text[idx + 2:]


def _write_initial(cv_text, greeting, company, industry, contact_title,
                   role_title, role_desc, is_open,
                   company_mission, company_focus, company_vision, company_notable,
                   linkedin_url: str = "", linkedin_snippet: str = "",
                   country: str = DEFAULT_COUNTRY) -> tuple:

    cfg = get_country_config(country)
    opener_options = ", ".join(f'"{w}"' for w in _OPENER_WORDS)
    if linkedin_url and linkedin_snippet:
        discovery_line = (
            f'Write: "I came across your profile on LinkedIn — " then reference something specific '
            f"from their profile below to explain what {company}'s work is compelling. "
            f"Use ONE phrase naturally: {opener_options}. "
            f"Do NOT start with 'I am writing to...'.\n"
            f"THEIR LINKEDIN PROFILE:\n{linkedin_snippet}"
        )
    elif linkedin_url:
        discovery_line = (
            f'Write: "I came across your profile on LinkedIn — " then continue naturally using '
            f"ONE of these phrases to describe why {company}'s work is compelling: {opener_options}. "
            f"Do NOT start with 'I am writing to...'."
        )
    else:
        discovery_line = (
            f"Start by mentioning how you came across {company} while researching "
            f"{cfg['research_intro']}. Use ONE of these phrases naturally to "
            f"describe why their work is compelling: {opener_options}. "
            f"Do NOT start with 'I am writing to...'."
        )

    if is_open:
        subject = f"Open Application | {CANDIDATE_NAME}"
        role_line = f"open application to **{company}**"
        para1_instruction = (
            f"Para 1 (CRITICAL — must open with how you found this person/company, NOT 'I am writing to...'):\n"
            f"  Sentence 1: {discovery_line}\n"
            f"  Sentence 2: Briefly state this is an open application and reference {company}'s "
            f"mission/focus to show genuine interest.\n"
            f"  Sentence 3: One sentence summarising MEng + years of experience + core areas."
        )
    else:
        subject = f"Application - {role_title} | {CANDIDATE_NAME}"
        role_line = f"the **{role_title}** position at **{company}**"
        para1_instruction = (
            f"Para 1 (CRITICAL — must open with how you found this person/company, NOT 'I am writing to...'):\n"
            f"  Sentence 1: {discovery_line}\n"
            f"  Sentence 2: State you are writing to apply for the **{role_title}** role at **{company}**.\n"
            f"  Sentence 3: One sentence connecting your background to why this role is a natural fit."
        )

    context = "\n".join(filter(None, [
        f"Industry: {industry}" if industry else "",
        f"Recipient's role: {contact_title}" if contact_title else "",
        f"Role description: {role_desc}" if role_desc else "",
        f"Company mission: {company_mission}" if company_mission else "",
        f"Company focus areas: {company_focus}" if company_focus else "",
        f"Company vision: {company_vision}" if company_vision else "",
        f"Company notable work: {company_notable}" if company_notable else "",
    ]))

    if _is_hr_recipient(contact_title):
        recipient_guidance = f"""
RECIPIENT CONTEXT — CRITICAL:
{contact_title} is in HR/People/Recruiting. You may use normal technical terms about Prateek's own work.
However, do NOT try to connect Prateek's engineering background to the recipient's HR/recruiting skills.
Do not write lines like "I see you've worked in talent acquisition, which resonates with my manufacturing background" — that is awkward and irrelevant.
When referencing the recipient's LinkedIn profile, reference only their company or industry context, not their personal HR/recruiting skillset.
"""
    else:
        recipient_guidance = ""

    prompt = f"""You are writing a job application email on behalf of Prateek Sahni.

Study these two example emails for tone, structure, and level of detail. IMPORTANT OVERRIDE:
- Use greeting "{greeting}" (not "Dear")
- Para 1 MUST NOT start with "I am writing to..." — it must open with how you came across the recipient/company (see Para 1 instruction below)

EXAMPLE 1 (for body style — ignore their outdated opening):
Hi Hiring Manager,

I am writing to apply for the **Additive Manufacturing Technician** position at **SGD 3D**. As an engineer with direct hands-on experience across all four core AM technologies — SLA, SLS, SLM, and FDM — and a strong foundation in post-processing and quality optimisation, I am enthusiastic about contributing to your team in Nottingham.

During my time as an **Additive Manufacturing Engineer at Irish Manufacturing Research (IMR, Mullingar, Ireland)**, I optimised AM workflows across SLA, SLS, SLM, and FDM platforms, reducing production time by 20% through systematic process improvements. I conducted Design of Experiments (DOE) to enhance surface finish quality, successfully achieving sub-2-micron Ra — a result that required precise control of post-processing steps, build parameters, and material behaviour.

I also standardised R&D workflows by authoring engineering SOPs, risk assessments, and quality documentation, ensuring repeatable, audit-ready processes. My MEng in Mechanical and Manufacturing Engineering (Dublin City University, First Class Honours) underpins my technical rigour.

I am keen to bring this hands-on AM expertise to SGD 3D and support your production and dispatch operations. Please find my CV attached. I look forward to hearing from you.

EXAMPLE 2 (for body style — ignore their outdated opening):
Hi Hiring Manager,

I am writing to apply for the **Lead Manufacturing Engineer** position at **Densix**. With an MEng in Mechanical and Manufacturing Engineering (Dublin City University, First Class Honours) and over five years of experience spanning additive manufacturing, process engineering, and project management, I am well-positioned to bridge the gap between your R&D prototypes and scalable commercial production.

In my current role as **Project Manager at B2B Growth Hub** (Southampton, UK), I have driven process automation initiatives that reduced operational costs by £15k in three months and improved cross-functional workflow efficiency by 30%. Prior to this, as a **Senior Process Engineer at Precise Axis**, I led end-to-end process development — managing PPAP validation, IQ/OQ/PQ, and technical transfer at scale-up from lab to high-volume production. This is directly analogous to what you need as Densix transitions novel power converter technology from R&D prototype to commercial build phases.

My earlier experience as an **Additive Manufacturing Engineer at Irish Manufacturing Research (IMR)** involved optimising SLA, SLS, SLM, and FDM workflows to reduce production time by 20% and achieving sub-2-micron Ra surface finishes through systematic DOE. I have authored engineering SOPs, FMEA risk assessments, and process documentation that have become team standards.

I am proficient in **SolidWorks, NX, and Ansys**, and have extensive experience with Lean Six Sigma, Statistical Process Control, and Root Cause Analysis (DMAIC, 8D). I have also managed supplier relationships for machined components and specialty materials throughout my career.

I am excited by the opportunity to contribute to Densix's mission of scaling transformative power technology into reliable production systems. Please find my CV attached. I would welcome the chance to discuss further at your earliest convenience.

---

NOW WRITE A NEW EMAIL:

Greeting: {greeting}
Applying for: {role_line}
{context}

CANDIDATE'S CV:
{cv_text}
{recipient_guidance}
MANDATORY structure — follow this exactly. Each paragraph MUST be 2-3 sentences maximum. Be specific and concise — no padding.

{para1_instruction}

Para 2 (MANDATORY — 3 sentences max): Current role: **Project Manager at B2B Growth Hub** (Southampton, UK). One sentence of specific achievement with a metric. One sentence connecting it directly to {company}.

Para 3 (MANDATORY — 3 sentences max): Previous role: **Senior Process Engineer at Precise Axis**. One sentence of specific achievement with metric (PPAP, IQ/OQ/PQ, scale-up). One sentence connecting to {company}'s work.

Para 4 (MANDATORY — 3 sentences max): Earlier role: **Additive Manufacturing Engineer at Irish Manufacturing Research (IMR, Mullingar, Ireland)**. One sentence of specific AM achievement (DOE, SLA/SLS/SLM/FDM, surface finish). One sentence connecting to {company}'s technology or manufacturing focus.

Para 5 (closing — 2 sentences): One sentence of genuine enthusiasm for {company}'s specific mission or work. "Please find my CV attached. Thank you for taking the time to read — should you feel my background could be of value, I would be glad to hear from you."

{_BOLD_RULES}
{_STYLE_RULES}

Return ONLY the email body paragraphs, starting with the greeting.
"""
    return _ask_claude(prompt), subject


def _write_followup_1(cv_text, greeting, company, industry, role_title, is_open, contact_title: str = "") -> tuple:
    if is_open:
        subject = f"Following Up - Open Application | {CANDIDATE_NAME}"
        ref = f"open application to **{company}**"
    else:
        subject = f"Following Up - {role_title} | {CANDIDATE_NAME}"
        ref = f"application for the **{role_title}** role at **{company}**"

    industry_line = f"Company industry: {industry}" if industry else ""

    if _is_hr_recipient(contact_title):
        hr_note = (
            f"\nRECIPIENT NOTE: {contact_title} is in HR/Recruiting. "
            "Do not connect Prateek's engineering skills to the recipient's HR/recruiting background — that reads as awkward. "
            "Technical terms about Prateek's own work are fine."
        )
    else:
        hr_note = ""

    prompt = f"""Write a follow-up job application email on behalf of Prateek Sahni.

Greeting: {greeting}
Following up on: {ref}
{industry_line}{hr_note}

CANDIDATE'S CV:
{cv_text}

Structure (2 paragraphs, 2-3 sentences each — concise and specific):
- Para 1: One sentence referencing the previous email. One or two sentences adding a new specific angle not in the first email — a different real role/skill/achievement drawn directly from the CV above, with its real location (e.g. Southampton, UK / New Delhi, India / Mullingar, Ireland — use whichever is actually attached to that role in the CV, not a guess).
- Para 2: One sentence of continued interest. End with "I appreciate you taking the time to consider my application — please do feel free to reach out if you think there could be a fit."

{_BOLD_RULES}
{_STYLE_RULES}

Return ONLY the email body starting with the greeting.
"""
    return _ask_claude(prompt), subject


def _write_followup_2(cv_text, greeting, company, role_title, is_open, contact_title: str = "") -> tuple:
    if is_open:
        subject = f"Final Follow-Up - Open Application | {CANDIDATE_NAME}"
        ref = f"open application to **{company}**"
    else:
        subject = f"Final Follow-Up - {role_title} | {CANDIDATE_NAME}"
        ref = f"application for the **{role_title}** role at **{company}**"

    if _is_hr_recipient(contact_title):
        hr_note = (
            f"\nRECIPIENT NOTE: {contact_title} is in HR/Recruiting. "
            "Do not connect Prateek's engineering skills to the recipient's HR/recruiting background."
        )
    else:
        hr_note = ""

    prompt = f"""Write a brief final follow-up email on behalf of Prateek Sahni.

Greeting: {greeting}
Context: Third and final email regarding {ref}.{hr_note}

CANDIDATE'S CV (for reference only — this email should not need new factual claims, but do not
contradict it if you reference anything):
{cv_text}

Structure:
- Para 1: Acknowledge this is a final follow-up. Keep it gracious and brief.
- Para 2: Leave the door open for future contact. End with "Thank you for your time across these emails — should anything change or a suitable opportunity arise in future, I would welcome the chance to connect."

{_BOLD_RULES}
{_STYLE_RULES}

Very short — 2 short paragraphs only.
Return ONLY the email body starting with the greeting.
"""
    return _ask_claude(prompt), subject
