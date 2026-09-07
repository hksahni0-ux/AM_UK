"""
CVAgent: writes the master CV's summary, achievement bullets, and skills
completely from scratch for a specific target role, while keeping every fact
real and the output to one page.

Two-step process, on purpose: first every original bullet and the summary are
distilled down to bare keyword/fact tags via `_extract_facts` (action, method,
metric — no grammar, no sentence structure). Only those tags, never the original
sentences, are shown to the CV-writing call. Giving a model the original wording
"for reference, don't copy it" still anchors it — it echoes nearby phrasing
regardless of instructions — so the fix is to never show it the phrasing at all.

Rewritten from scratch: the PROFESSIONAL SUMMARY, every achievement bullet under
WORK EXPERIENCE (detected via numbering/bullet formatting), and the SKILLS line
(full rewrite, not just reordering — still one comma-separated line, same format).

Job titles, companies, dates, and EDUCATION are copied verbatim from the master
docx and never sent to Claude — they are facts, not framing.

Fact safety is checked holistically per job, not bullet-by-bullet: every numeric/
currency figure (£15k, 30%, 20%, etc.) appearing in a job's rewritten bullets must
trace back to a figure that appeared somewhere in that SAME job's original tags —
Claude is free to move a figure to a different bullet within the job while
rewriting around it, but never to a different job, and never invent one that
isn't real. The summary is checked against the whole CV, since it synthesises
across all jobs. If a genuinely new/misattributed figure shows up, the whole
attempt is retried with that called out; after enough failed attempts, tailoring
is abandoned and the caller falls back to the original CV.

The one-page constraint is enforced for real: after each rewrite the docx is
converted to PDF and its page count is checked. If it runs over one page, Claude
is asked again with an explicit "shorten it" instruction, up to a few attempts;
if it still won't fit, tailoring is abandoned and the caller falls back to the
original CV rather than send something overflowing.

Open applications (no specific role matched) are sent with the original CV —
there's nothing to tailor toward.

The tailored .docx is converted to PDF via a throwaway Google Doc (upload with
auto-convert, export as PDF, delete) using the same OAuth credentials already
used for Gmail/Sheets — no local office suite required. The output file is
always named the same as the master CV (e.g. "_PRATEEK CV_2026.pdf"), just
stored in a per-company subfolder — so it always looks the same to a recipient.
"""

import json
import logging
import os
import re
from functools import lru_cache
from typing import Optional

import docx
import pdfplumber
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from agents import llm_client
from config.settings import CV_MASTER_DOCX, CV_GENERATED_DIR, GOOGLE_SCOPES, SENDERS

log = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_GDOC_MIME = "application/vnd.google-apps.document"
_PDF_MIME = "application/pdf"

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NUMPR_XPATH = f".//{_W_NS}numPr"

_SUMMARY_HEADING = "PROFESSIONAL SUMMARY"
_EXPERIENCE_HEADING = "WORK EXPERIENCE"
_SKILLS_HEADING = "SKILLS"

# Only flags achievement-metric numbers (currency, %, or k-suffixed) — a bare digit like the "1-6" in
# "TRL1-6" isn't a fact worth fact-checking and produces false positives if matched.
_NUMERIC_TOKEN_RE = re.compile(
    r"[£$€]\s*\d[\d,.]*\s*k?|\d[\d,.]*\s*%|\d[\d,.]*\s*k\b", re.IGNORECASE
)

_TOOL_SCHEMA = {
    "name": "tailored_cv",
    "description": "Tailored CV summary, work-experience bullets, and skills priority order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Rewritten skills list, most-relevant-to-the-role first. Only skills/tools genuinely evidenced by the CV's experience — no inventions.",
            },
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["bullets"],
                },
            },
        },
        "required": ["summary", "skills", "jobs"],
    },
}

_FACT_EXTRACT_SCHEMA = {
    "name": "extracted_facts",
    "description": "Terse keyword/fact tags extracted from CV bullets and summary, stripped of all original sentence structure and wording.",
    "input_schema": {
        "type": "object",
        "properties": {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bullet_tags": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                            "description": "One tag-list per original bullet, same order, same count — each tag-list is 2-5 short keyword/phrase fragments (action, method/tool, metric), never a grammatical sentence.",
                        },
                    },
                    "required": ["bullet_tags"],
                },
            },
            "summary_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "6-10 standalone keyword tags summarising the whole career (years of experience, methodologies, certifications, standout figures) for building a brand new summary.",
            },
        },
        "required": ["jobs", "summary_tags"],
    },
}


def _extract_facts(job_blocks: list, summary_original: str) -> dict:
    """
    Distills each original bullet down to bare keyword/fact tags, discarding all
    prose — so the CV-writing call downstream never sees a ready-made sentence to
    echo. This is what actually stops the rewrite from drifting back toward the
    original's phrasing: the anchor is the wording sitting in context, not the
    instruction not to copy it.
    """
    jobs_payload = [{"bullets": b["_bullet_texts"]} for b in job_blocks]
    prompt = f"""Extract the essential facts from each CV bullet below as terse keyword/phrase tags —
NOT full sentences, NOT grammatical prose, NOT the original wording. Strip away every connecting word,
verb, and sentence structure. For each bullet, output 2-5 short fragments: the core action/subject (a
couple of words), any method/tool/technology named, and any number/percentage/currency figure as its
own separate tag (e.g. from "Cut operational costs by £15k in 3 months using lean process development"
extract ["cost reduction", "£15k", "3 months", "lean process development"] — not a sentence).

Also extract 6-10 standalone keyword tags summarising the whole career (years of experience,
methodologies, certifications, standout figures) for building a brand new summary from scratch.

BULLETS BY JOB (in order):
{json.dumps(jobs_payload, indent=2)}

CAREER SUMMARY (extract tags only, do not preserve wording):
{summary_original}

Call the extracted_facts tool. jobs[] must have the same number of entries, in the same order, and
each entry's bullet_tags[] must have exactly as many tag-lists as that job has bullets above."""

    return llm_client.complete_tool("cv_extract_facts", prompt, _FACT_EXTRACT_SCHEMA, max_tokens=2000)


# ── docx structure extraction ─────────────────────────────────────────────────

def _is_bullet(paragraph) -> bool:
    return paragraph._p.find(_NUMPR_XPATH) is not None


def _extract_structure(doc):
    """
    Walks the master doc and returns:
      summary_idx        — paragraph index of the summary text
      job_blocks         — [{"context": "Title — Company", "bullet_idxs": [...]}]
      skills_idx         — paragraph index of the skills line
      skills_items       — the skills line split into individual terms
      skills_suffix      — trailing punctuation (e.g. ".") stripped before splitting
    """
    section = None
    summary_idx = None
    skills_idx = None
    job_blocks = []
    current_block = None
    recent_nonbullet = []  # rolling window of last non-blank, non-bullet paragraph texts

    for i, p in enumerate(doc.paragraphs):
        text = p.text.strip()

        if p.style.name == "Heading 2":
            section = text
            current_block = None
            recent_nonbullet = []
            continue

        if section == _SUMMARY_HEADING:
            if text and summary_idx is None:
                summary_idx = i

        elif section == _EXPERIENCE_HEADING:
            if _is_bullet(p):
                if current_block is None:
                    context = " — ".join(recent_nonbullet[-2:]) if recent_nonbullet else "Role"
                    current_block = {"context": context, "bullet_idxs": []}
                    job_blocks.append(current_block)
                current_block["bullet_idxs"].append(i)
            elif text:
                recent_nonbullet.append(text)
                current_block = None  # next bullet run starts a fresh block
            else:
                current_block = None

        elif section == _SKILLS_HEADING:
            if text and skills_idx is None:
                skills_idx = i

    skills_items, skills_suffix = [], ""
    if skills_idx is not None:
        raw = doc.paragraphs[skills_idx].text.strip()
        skills_suffix = "." if raw.endswith(".") else ""
        skills_items = [s.strip() for s in _split_top_level(raw.rstrip(".")) if s.strip()]

    return summary_idx, job_blocks, skills_idx, skills_items, skills_suffix


def _split_top_level(text: str, sep: str = ",") -> list:
    """Comma-split that doesn't split inside parentheses, e.g. keeps
    "Root Cause Analysis (CAPAs, 5-Whys, DMAIC, 8Ds)" as one item."""
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


# ── validation guardrails ──────────────────────────────────────────────────────

def _normalize_token(tok: str):
    """£15,000 and £15k must compare equal — normalize to (currency, is_percent, value)."""
    tok = tok.strip()
    is_percent = tok.endswith("%")
    if is_percent:
        tok = tok[:-1].strip()
    currency = tok[0] if tok and tok[0] in "£$€" else ""
    if currency:
        tok = tok[1:]
    is_k = tok.lower().endswith("k")
    if is_k:
        tok = tok[:-1]
    tok = tok.replace(",", "")
    try:
        value = float(tok)
    except ValueError:
        return None
    if is_k:
        value *= 1000
    return (currency, is_percent, round(value, 3))


def _numeric_tokens(text: str) -> set:
    normalized = (_normalize_token(t) for t in _NUMERIC_TOKEN_RE.findall(text))
    return {n for n in normalized if n is not None}


def _fabricated_figures(original_full_text: str, new_full_text: str) -> set:
    """Any numeric/currency figure in the new CV that doesn't trace back to the
    original anywhere — checked holistically, not bullet-by-bullet, so Claude is
    free to restructure which sentence a figure lands in."""
    return _numeric_tokens(new_full_text) - _numeric_tokens(original_full_text)


_STOPWORDS = frozenset({
    "and", "the", "of", "in", "to", "with", "using", "by", "for", "at", "a", "on", "via", "or",
    "from", "an", "as", "is", "are", "this", "that", "into", "across", "within", "over", "per",
})


def _content_tokens(text: str) -> set:
    return {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", text) if len(t) > 1 and t.lower() not in _STOPWORDS}


def _skills_vocabulary(base_doc, summary_idx, job_blocks, skills_items) -> set:
    """Every real word/acronym/tool-name ever used in the true CV — bullets, summary, and the
    original skills line — so a skill can be relabeled with the JD's own words but not invented
    from nothing. Scoped to skills specifically: skill entries are short technical noun-phrases
    with almost no connective words, so a hard token-overlap check has few false positives there
    (unlike full sentences, where framing words would trigger constant false rejections)."""
    vocab = _content_tokens(_collect_text(base_doc, summary_idx, job_blocks))
    vocab |= _content_tokens(", ".join(skills_items))
    return vocab


def _unevidenced_skills(vocabulary: set, new_skills: list) -> list:
    """Skill entries that share zero real-CV vocabulary — e.g. 'Injection Molding' when nothing
    in the true CV ever mentions injection or molding. A genuine relabel (JD's 'Risk Management'
    for the original 'FMEA & Risk Assessment') always shares at least one token; this only catches
    something introduced from nothing."""
    flagged = []
    for skill in new_skills:
        tokens = _content_tokens(str(skill))
        if tokens and not (tokens & vocabulary):
            flagged.append(skill)
    return flagged


# ── Claude call ────────────────────────────────────────────────────────────────

def _ask_claude(summary_word_ceiling_base, job_blocks, skills_items, role_title, role_desc, company,
                 industry, summary_tags: list, shorten_hint: str = "", length_scale: float = 1.0) -> dict:
    jobs_payload = [
        {
            "context": b["context"],
            "bullet_slots": len(b["_bullet_texts"]),
            "max_characters_per_slot": [max(20, int(len(t) * length_scale)) for t in b["_bullet_texts"]],
            "fact_tags_per_bullet": b["_fact_tags"],
        }
        for b in job_blocks
    ]
    summary_word_ceiling = max(15, int(summary_word_ceiling_base * length_scale))
    if role_desc:
        jd_instruction = f"""
STEP 1 — Before rewriting anything, read the job description below and pull out the specific
keywords it uses: tools, technologies, methodologies, certifications, and skill terms (e.g. "risk
management", "TRL1-6", "lean manufacturing", "SPC", specific standards or software names).

STEP 2 — For every keyword you found that has a genuine match somewhere in the original CV (same
skill/tool/experience, possibly worded differently), use the JOB DESCRIPTION'S wording for it in
your rewrite — in the summary, the relevant bullet, and/or the skills reordering. This is what
actually helps with ATS keyword matching — generic "match the tone" rewriting is not enough.

Never use a keyword from the job description that has no real match in the original CV — that
would be fabrication, not tailoring.

JOB DESCRIPTION:
{role_desc}
"""
    else:
        jd_instruction = "\nNo job description text was available — tailor based on the role title alone.\n"

    prompt = f"""Write Prateek Sahni's CV completely from scratch for this specific job application. You
have never seen the original CV's wording — only the template's skeleton (section headings, job titles,
employers, dates, education — those are unchangeable historical facts) and the bare fact tags below.
There is no original sentence anywhere in this prompt to echo, because there isn't one: every bullet and
the summary must be built as new prose from these tags alone.

Each tag list under "fact_tags_per_bullet" is bare keyword/fact fragments (action, method/tool, metric) —
not a sentence, not connected prose. Turn each set of tags into a normal, fluent CV sentence as if writing
it for the first time; there is no original phrasing to preserve because none was given to you.

You decide freely how to use each job's fixed number of bullet_slots for THIS role: merge two related
tag-sets into one sharper bullet, give one especially relevant tag-set its own bullet with more depth,
drop emphasis on what's less relevant, present them in whatever order best sells this candidate for this
role — they don't have to keep the grouping or order listed below. Lead every bullet with the
outcome/impact. Same for the summary: build it entirely around why Prateek fits THIS role, leading with
the single most relevant qualification — not a generic profile with tailoring bolted on.

CRITICAL — a bullet is not done just because the fact is stated in new words. Restating "£15k saved via
lean process development" more punchily is NOT tailoring by itself — every bullet must also make its
relevance to THIS role explicit: use a term straight from the job description, name the transferable
skill this role needs, or frame the achievement as evidence of exactly what {company} is looking for.
If a bullet could be dropped unchanged into an application for a completely different role, it hasn't
been tailored yet — go back and connect it to {role_title} specifically. Do this for every bullet, not
just one or two — the whole CV should read like it was written by someone who has read this job posting
closely, not like a generic CV that happens to state real numbers.

This does NOT mean making bullets longer — you're already length-constrained. It means choosing which
word does the work: e.g. write "...cutting operational costs" as "...cutting costs during high-volume
scale-up" if that's the JD's own framing, not "...cutting operational costs, which is also relevant to
scale-up work" as a bolted-on extra clause. Swap generic words for the JD's specific ones; don't add words.

The one hard rule, because it's what actually protects an interview once it's landed: do not invent facts.
Every fact you state for a given job must trace back to something in THAT SAME job's own
"fact_tags_per_bullet" — never attribute an achievement to a different employer than where it really
happened, and never invent one that isn't tagged there. Every number, percentage, and currency figure you
use must be one that genuinely appears among that job's own tags — never a figure that isn't real, and
nothing he'd struggle to back up when asked about it in an interview.

The master CV is already at exactly one page with no spare room, so length is a HARD CEILING, not a
target — going over means the CV silently gets rejected and the original is sent instead. For the
summary: {summary_word_ceiling} words MAXIMUM (shorter is fine). For each bullet: its slot's
max_characters_per_slot value is the MAXIMUM length for whatever you put in that slot (count characters
as you write; shorter is fine). Never exceed these — use the space as effectively as possible within it.
{shorten_hint}
{jd_instruction}
Target role: {role_title}
Target company: {company}
Industry: {industry or "unspecified"}

Write a brand new summary from scratch (max {summary_word_ceiling} words) using only these career-level
fact tags — no original summary sentence exists to reference:
{json.dumps(summary_tags, indent=2)}

WORK HISTORY (grouped by job — each job's fact_tags_per_bullet are bare keyword fragments, self-contained
per job; return exactly bullet_slots brand-new bullets per job, each within its slot's character ceiling,
using only that job's own tags):
{json.dumps(jobs_payload, indent=2)}

SKILLS SOURCE MATERIAL (raw terms, not a template to reorder — write a brand new comma-separated skills
line for {role_title} from scratch: for each real skill/tool below that has a genuine counterpart in the
job description, NAME IT using the job description's own term rather than the generic original label
(e.g. if the JD says "risk management" and the original label is "FMEA & Risk Assessment", lead with
"Risk Management" — same real skill, JD's own words); group, reorder, and drop less-relevant items freely.
Every skill/tool you list must genuinely be evidenced by the work history above — no inventing
certifications or tools Prateek doesn't have):
{json.dumps(skills_items, indent=2)}

Call the tailored_cv tool with your rewrite. jobs[] must have the same number of entries, in the
same order, and each entry's bullets[] must have exactly bullet_slots items, as WORK HISTORY above."""

    return llm_client.complete_tool("cv_write", prompt, _TOOL_SCHEMA, max_tokens=4000)


# ── docx → PDF via Google Drive ────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _drive_service():
    creds = Credentials.from_authorized_user_file(SENDERS[0]["token_file"], GOOGLE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(f"Invalid credentials in {SENDERS[0]['token_file']}. Run setup_auth.py.")
    return build("drive", "v3", credentials=creds)


def docx_to_pdf(docx_path: str) -> Optional[str]:
    """
    Converts a .docx to .pdf via a throwaway Google Doc (upload → auto-convert →
    export as PDF → delete). Returns the PDF path, or None on any failure — caller
    should fall back to the static master CV PDF.
    """
    drive = _drive_service()
    file_id = None
    try:
        media = MediaFileUpload(docx_path, mimetype=_DOCX_MIME)
        uploaded = drive.files().create(
            body={"name": os.path.basename(docx_path), "mimeType": _GDOC_MIME},
            media_body=media,
            fields="id",
        ).execute()
        file_id = uploaded["id"]

        pdf_bytes = drive.files().export(fileId=file_id, mimeType=_PDF_MIME).execute()
        pdf_path = os.path.splitext(docx_path)[0] + ".pdf"
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return pdf_path
    except Exception as exc:
        log.error("CVAgent: docx→PDF conversion failed for %s: %s", docx_path, exc)
        return None
    finally:
        if file_id:
            try:
                drive.files().delete(fileId=file_id).execute()
            except Exception as exc:
                log.warning("CVAgent: failed to clean up temp Google Doc %s: %s", file_id, exc)


_MAX_ATTEMPTS = 4


def _pdf_page_count(pdf_path: str) -> int:
    with pdfplumber.open(pdf_path) as pdf:
        return len(pdf.pages)


def _apply_rewrite(doc, summary_idx, summary_original, job_blocks, skills_idx, skills_items,
                    skills_suffix, result: dict) -> None:
    """Applies Claude's rewrite as-is — fact-checking happens holistically afterward,
    not per-field, so this only guards against a missing/empty field in a malformed
    response (falls back to the original for that field)."""
    if summary_idx is not None:
        new_summary = result.get("summary")
        text = new_summary if isinstance(new_summary, str) and new_summary.strip() else summary_original
        _set_paragraph_text(doc.paragraphs[summary_idx], text)

    proposed_jobs = result.get("jobs") or []
    for block, proposed in zip(job_blocks, proposed_jobs):
        proposed_bullets = proposed.get("bullets") or []
        padded = proposed_bullets + block["_bullet_texts"][len(proposed_bullets):]  # pad if short
        for idx, orig_text, new_text in zip(block["bullet_idxs"], block["_bullet_texts"], padded):
            text = new_text if isinstance(new_text, str) and new_text.strip() else orig_text
            _set_paragraph_text(doc.paragraphs[idx], text)

    if skills_idx is not None and skills_items:
        new_skills = result.get("skills")
        items = new_skills if isinstance(new_skills, list) and new_skills else skills_items
        text = ", ".join(str(s).strip() for s in items if str(s).strip()) + skills_suffix
        _set_paragraph_text(doc.paragraphs[skills_idx], text)


def _collect_text(doc, summary_idx, job_blocks) -> str:
    parts = []
    if summary_idx is not None:
        parts.append(doc.paragraphs[summary_idx].text)
    for block in job_blocks:
        parts.extend(doc.paragraphs[i].text for i in block["bullet_idxs"])
    return " ".join(parts)


def _job_text(doc, block) -> str:
    return " ".join(doc.paragraphs[i].text for i in block["bullet_idxs"])


# ── Public API ──────────────────────────────────────────────────────────────────

def tailor_cv(row: dict, job: dict, row_number, out_dir: Optional[str] = None) -> Optional[str]:
    """
    Generates a role-tailored copy of the master CV, converted to PDF, verified
    to fit one page. Returns the PDF path, or None if there's no specific role
    to tailor toward (open application), tailoring failed, PDF conversion
    failed, or it couldn't be made to fit one page after retries — caller
    should fall back to the static master CV PDF in every None case; this must
    never block a send.

    out_dir: where to write the generated docx/pdf. Defaults to a per-company
    subfolder under CV_GENERATED_DIR (cv/generated/) for manual review via the
    test scripts. Production callers should pass a temp directory instead —
    the sent Gmail message is the durable copy; nothing needs to persist here.
    """
    if job.get("is_open_application", True):
        log.info("CVAgent: open application — sending original CV, no tailoring")
        return None

    try:
        base_doc = docx.Document(CV_MASTER_DOCX)
        summary_idx, job_blocks, skills_idx, skills_items, skills_suffix = _extract_structure(base_doc)
        for block in job_blocks:
            block["_bullet_texts"] = [base_doc.paragraphs[i].text for i in block["bullet_idxs"]]
        summary_original = base_doc.paragraphs[summary_idx].text if summary_idx is not None else ""

        if out_dir is None:
            os.makedirs(CV_GENERATED_DIR, exist_ok=True)
            company_slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(row.get("company_name", "company"))).strip("_")
            out_dir = os.path.join(CV_GENERATED_DIR, f"{company_slug}_{row_number}")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, os.path.basename(CV_MASTER_DOCX))

        original_full_text = _collect_text(base_doc, summary_idx, job_blocks)
        summary_word_ceiling_base = len(summary_original.split()) if summary_idx is not None else 60
        skills_vocabulary = _skills_vocabulary(base_doc, summary_idx, job_blocks, skills_items)

        facts = _extract_facts(job_blocks, summary_original)
        for block, proposed_job in zip(job_blocks, facts.get("jobs") or []):
            tags = proposed_job.get("bullet_tags") or []
            # Fallback per-bullet if extraction came back short/malformed: use the raw bullet as its
            # own one-item "tag" rather than lose the fact entirely.
            block["_fact_tags"] = [
                tags[i] if i < len(tags) and tags[i] else [orig]
                for i, orig in enumerate(block["_bullet_texts"])
            ]
        summary_tags = facts.get("summary_tags") or [summary_original]

        retry_hint = ""
        length_scale = 1.0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            result = _ask_claude(
                summary_word_ceiling_base=summary_word_ceiling_base,
                job_blocks=job_blocks,
                skills_items=skills_items,
                role_title=job.get("role_title", "Open Application"),
                role_desc=job.get("role_description", ""),
                company=row.get("company_name", ""),
                industry=row.get("company_industry", ""),
                summary_tags=summary_tags,
                shorten_hint=retry_hint,
                length_scale=length_scale,
            )

            attempt_doc = docx.Document(CV_MASTER_DOCX)
            _apply_rewrite(attempt_doc, summary_idx, summary_original, job_blocks,
                           skills_idx, skills_items, skills_suffix, result)

            fabricated = set()
            for block in job_blocks:
                fabricated |= _fabricated_figures(_job_text(base_doc, block), _job_text(attempt_doc, block))
            if summary_idx is not None:
                fabricated |= _fabricated_figures(original_full_text, attempt_doc.paragraphs[summary_idx].text)

            if fabricated:
                log.warning("CVAgent: attempt %d introduced figures not traceable to the right job: %s, retrying",
                            attempt, fabricated)
                retry_hint = (
                    f"IMPORTANT: your previous attempt introduced these figures, which do NOT appear "
                    f"in the CV at all, or were attributed to the wrong job: {sorted(fabricated)}. Every "
                    f"number/percentage/currency figure in a job's bullets must come from THAT job's own "
                    f"facts_achieved_in_this_job list — remove or fix these."
                )
                continue

            if skills_idx is not None:
                new_skills_items = _split_top_level(attempt_doc.paragraphs[skills_idx].text.rstrip("."))
                unevidenced = _unevidenced_skills(skills_vocabulary, new_skills_items)
                if unevidenced:
                    log.warning("CVAgent: attempt %d listed skills with no basis in the real CV: %s, retrying",
                                attempt, unevidenced)
                    retry_hint = (
                        f"IMPORTANT: your previous attempt listed these skills, which have no basis anywhere "
                        f"in Prateek's real CV: {unevidenced}. Every skill must be a real one he has, optionally "
                        f"relabelled with the job description's own term — never a skill introduced from nothing "
                        f"(e.g. if the role wants injection moulding and he has no injection moulding experience, "
                        f"do not claim it — lean on genuinely adjacent real skills instead, like additive "
                        f"manufacturing process scale-up or high-volume production validation)."
                    )
                    continue

            attempt_doc.save(out_path)
            pdf_path = docx_to_pdf(out_path)
            if not pdf_path:
                log.error("CVAgent: PDF conversion failed, caller should fall back to master CV")
                return None

            pages = _pdf_page_count(pdf_path)
            if pages <= 1:
                log.info("CVAgent: tailored CV fits one page (attempt %d) — %s", attempt, pdf_path)
                return pdf_path

            length_scale *= 0.85
            log.warning("CVAgent: attempt %d produced a %d-page CV, retrying at %.0f%% length", attempt, pages, length_scale * 100)
            retry_hint = (
                f"IMPORTANT: your previous attempt rendered to {pages} pages — it MUST fit exactly "
                f"one page. The character/word ceilings below have been tightened accordingly — treat "
                f"them as the real limit this time, not generous headroom; keeping every fact and figure real."
            )

        log.error("CVAgent: could not produce a fact-safe, one-page CV after %d attempts, "
                   "falling back to master CV", _MAX_ATTEMPTS)
        return None

    except Exception as exc:
        log.error("CVAgent: tailoring failed, caller should fall back to master CV: %s", exc)
        return None


def _set_paragraph_text(paragraph, text: str) -> None:
    """Replace a paragraph's visible text in its first run, preserving formatting."""
    if not paragraph.runs:
        paragraph.add_run(text)
        return
    paragraph.runs[0].text = text
    for extra in paragraph.runs[1:]:
        extra.text = ""
