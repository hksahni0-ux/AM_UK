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

The one-page constraint is enforced before rendering as well as after: every
bullet must fit one printed line (the master's longest bullet sets the budget),
over-long ones get one targeted shortening pass and otherwise keep the original
wording, and the skills line is trimmed to the master's length. Then the docx is
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


# Standards/certification codes ("ISO 13485", "AS9100", "IATF 16949") — the figure check above
# can't see these, and a tailored CV once claimed "ISO 9001/AS9100" straight from the job ad.
_STANDARD_RE = re.compile(
    r"\b(?:ISO|IEC|AS|EN|IATF|ASTM|BS|ANSI|ASME|MIL|DIN|NADCAP|API)[\s\-]?\d{2,}(?:[\-:/]\d+)*",
    re.IGNORECASE,
)


def _standards(text: str) -> set:
    return {re.sub(r"[\s\-]", "", m.group(0)).upper() for m in _STANDARD_RE.finditer(text)}


# Words that legitimately appear in tailored prose without being a claim about experience.
_GENERIC_OK = frozenset({
    "role", "roles", "team", "teams", "experience", "experienced", "expert", "expertise",
    "proven", "record", "track", "years", "year", "strong", "skills", "skilled", "ability",
    "deliver", "delivered", "delivering", "drive", "driving", "drove", "lead", "leading", "led",
    "support", "supporting", "enable", "enabling", "ensure", "ensuring", "improve", "improving",
    "improvement", "improvements", "new", "high", "end", "key", "including", "focus", "focused",
    "across", "through", "based", "related", "relevant", "management", "manage", "managed",
    "managing", "engineer", "engineering", "senior", "results", "impact", "projects", "project",
})
# Credentials that must already be in the real CV to be claimed at all.
_CREDENTIAL_RE = re.compile(
    r"\b(black belt|green belt|yellow belt|chartered|certified|certification|ceng|pmp|prince2|"
    r"phd|licensed|accredited)\b", re.IGNORECASE)


def _stem(tok: str) -> str:
    if tok.endswith("ies") and len(tok) > 5:        # technologies → technology
        return tok[:-3] + "y"
    for suf in ("ization", "isation", "ations", "ation", "ments", "ment", "ings", "ing", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 4:
            return tok[:-len(suf)]
    return tok


def _jd_borrowed_terms(new_text: str, cv_text: str, jd_text: str) -> set:
    """
    Words the rewrite took from the job description that the real CV never uses —
    "catheter", "CFR", "LPBF": the model presenting the employer's domain as Prateek's
    own experience. Stemmed, so 'validation'/'validated' count as the same word.
    """
    cv = {_stem(t) for t in _content_tokens(cv_text)}
    jd = {_stem(t) for t in _content_tokens(jd_text)}
    borrowed = set()
    for tok in _content_tokens(new_text):
        st = _stem(tok)
        if tok in _GENERIC_OK or st in cv or st not in jd or tok.isdigit():
            continue
        borrowed.add(tok)
    return borrowed


def _well_formed(result: dict, job_blocks: list) -> bool:
    """The rewrite has one entry per job, each with a list of bullet strings — the NVIDIA
    models sometimes return jobs as bare strings or drop a job, which used to crash."""
    jobs = result.get("jobs") if isinstance(result, dict) else None
    if not isinstance(jobs, list) or len(jobs) != len(job_blocks):
        return False
    for j, block in zip(jobs, job_blocks):
        bullets = j.get("bullets") if isinstance(j, dict) else None
        if not isinstance(bullets, list) or len(bullets) != len(block["_bullet_texts"]) \
                or not all(isinstance(b, str) and b.strip() for b in bullets):
            return False
    return isinstance(result.get("summary", ""), str)


def _misattributed_bullets(job_blocks: list, result: dict, jd_text: str, cv_text: str = "") -> list:
    """
    Bullets that attach another part of the CV to this job — a Concentrix "response
    time / customer satisfaction" bullet landing under Precise Axis, or "real-time SPC
    monitoring" added to a job whose real bullets never mention SPC. The figure check
    can't see this when nothing numeric moved. A bullet is flagged when it uses 2+
    terms that exist elsewhere in the real CV (other jobs, summary, skills) but not in
    that job's own original bullets — and that the job ad didn't ask for in other words.
    """
    own = [{_stem(t) for t in _content_tokens(" ".join(b["_bullet_texts"]))} for b in job_blocks]
    elsewhere_all = {_stem(t) for t in _content_tokens(cv_text)} if cv_text else set().union(*own)
    jd = {_stem(t) for t in _content_tokens(jd_text)}
    flagged = []
    for ji, proposed in enumerate(result.get("jobs") or []):
        if ji >= len(own):
            break
        for text in proposed.get("bullets") or []:
            foreign = {t for t in _content_tokens(text)
                       if t not in _GENERIC_OK and _stem(t) in elsewhere_all
                       and _stem(t) not in own[ji] and _stem(t) not in jd}
            if len(foreign) >= 2:
                flagged.append((job_blocks[ji]["context"].split(" — ")[0].split("  ")[0], text, sorted(foreign)))
    return flagged


def _repair_honesty(result: dict, job_blocks: list, summary_original: str, cv_text: str,
                    jd_text: str, original_standards: set) -> int:
    """
    Surgical version of the honesty checks: instead of throwing away a whole rewrite
    because one bullet borrowed "catheter" from the job ad, put that one bullet back to
    its original wording (always true, always fits). A piece fails if it borrows job-ad
    terms the CV never uses, claims a credential or standard he doesn't have, carries
    another job's facts, or introduces subject-matter words found nowhere in the CV.
    Same for the summary (one new word tolerated); skills that fail are dropped.
    Returns how many pieces were repaired.
    """
    def dishonest(text: str, novel_allowed: int = 0) -> bool:
        return bool(_jd_borrowed_terms(text, cv_text, jd_text) or _invented_credentials(text, cv_text)
                    or (_standards(text) - original_standards)
                    or len(_novel_terms(text, cv_text)) > novel_allowed)

    repaired = 0
    moved = {(job_ctx, text) for job_ctx, text, _ in _misattributed_bullets(job_blocks, result, jd_text, cv_text)}
    for ji, (block, proposed) in enumerate(zip(job_blocks, result.get("jobs") or [])):
        ctx = block["context"].split(" — ")[0].split("  ")[0]
        bullets = proposed.get("bullets") or []
        for bi, text in enumerate(bullets):
            if bi < len(block["_bullet_texts"]) and (dishonest(text) or (ctx, text) in moved):
                bullets[bi] = block["_bullet_texts"][bi]
                repaired += 1
    summary = result.get("summary")
    if isinstance(summary, str) and summary_original and dishonest(summary, novel_allowed=1):
        result["summary"] = summary_original
        repaired += 1
    skills = result.get("skills")
    if isinstance(skills, list):
        kept = [x for x in skills if not dishonest(str(x))]
        repaired += len(skills) - len(kept)
        result["skills"] = kept
    return repaired


# Phrasing words a rewrite may introduce freely — how an achievement is told, not what it was.
_PHRASING_OK = frozenset({
    "gain", "gains", "reduction", "reductions", "saving", "savings", "boost", "lift", "cut", "cuts",
    "drive", "drove", "secure", "form", "accelerate", "raise", "raised", "achieve", "achieved",
    "deliver", "scores", "outcomes", "efficient", "efficiently", "measurable", "successful",
    "successfully", "consistent", "robust", "faster", "rapidly", "overall", "total", "direct",
    "directly", "efficiency", "hands", "via", "while", "both", "each",
})


def _novel_terms(text: str, cv_text: str) -> set:
    """
    Subject-matter words in a rewrite that the real CV never uses at all — "aerospace
    output", "rapid prototyping", "precision components": detail invented out of thin
    air rather than borrowed from the job ad. Verb forms (-ing/-ed) and general
    phrasing words are allowed; the claim lives in the nouns.
    """
    cv = {_stem(t) for t in _content_tokens(cv_text)}
    return {t for t in _content_tokens(text)
            if _stem(t) not in cv and t not in _GENERIC_OK and t not in _PHRASING_OK
            and not t.isdigit() and not re.search(r"(ing|ed)$", t)}


def _invented_credentials(new_text: str, cv_text: str) -> set:
    real = {m.lower() for m in _CREDENTIAL_RE.findall(cv_text)}
    return {m.lower() for m in _CREDENTIAL_RE.findall(new_text)} - real


_SHORTEN_SCHEMA = {
    "name": "shortened",
    "description": "Rewritten, shorter CV lines.",
    "input_schema": {
        "type": "object",
        "properties": {"lines": {
            "type": "array", "items": {"type": "string"},
            "description": "One rewritten line per input, same order, each within its max_length.",
        }},
        "required": ["lines"],
    },
}


def _shorten_lines(lines: list, limit: int) -> list:
    """
    Targeted call to bring specific over-long lines under `limit` characters. The
    NVIDIA models echo lines back unchanged when simply asked to "shorten to N
    characters" — telling them each line's current length and how much must go
    is what actually gets words removed. Aims a little under the limit for margin.
    """
    target = max(40, limit - 8)
    items = [{"line": l, "current_length": len(l), "max_length": target,
              "must_remove_at_least": max(1, len(l) - target)} for l in lines]
    prompt = (
        "These CV bullet lines are TOO LONG to fit on one printed line. Rewrite each one so it "
        "is no longer than its max_length characters (spaces count). You MUST actually remove "
        "words — returning a line unchanged or longer is a failure. Keep the figures (e.g. £30k, "
        "30%) and the main achievement; drop trailing clauses, adjectives and filler first. "
        "Never add anything new.\n\n" + json.dumps(items, indent=2, ensure_ascii=False)
    )
    out = llm_client.complete_tool("cv_write", prompt, _SHORTEN_SCHEMA, max_tokens=1500).get("lines") or []
    if len(out) != len(lines):
        return lines
    return [o if isinstance(o, str) and o.strip() else l for o, l in zip(out, lines)]


def _trim_trailing_clause(text: str, limit: int) -> str:
    """Deterministic last resort before reverting: drop trailing ', enabling …' /
    ' — …' clauses (where tailoring tends to bolt on its framing) until it fits."""
    while len(text) > limit:
        cut = max(text.rfind(", "), text.rfind(" — "), text.rfind(" - "))
        if cut < 30:
            break
        text = text[:cut].rstrip(" ,;—-") + "."
    return text


def _enforce_lengths(result: dict, job_blocks: list, bullet_limit: int, skills_limit: int,
                     summary_char_limit: int, summary_original: str) -> int:
    """
    Makes the rewrite physically fit before it's ever rendered: over-long bullets get
    up to two targeted shortening passes, then a trailing-clause trim, and any still
    too long fall back to that slot's original bullet (guaranteed to fit, and a real
    fact); the skills line is cut from
    the end (it's ordered most-relevant first); an over-long summary falls back to the
    original. Returns how many bullets were reverted to the original.
    """
    over = []  # (job_i, slot_i, text)
    for ji, (block, proposed) in enumerate(zip(job_blocks, result.get("jobs") or [])):
        for bi, text in enumerate(proposed.get("bullets") or []):
            if isinstance(text, str) and len(text) > bullet_limit:
                over.append((ji, bi, text))
    summary = result.get("summary") or ""
    summary_over = isinstance(summary, str) and len(summary) > summary_char_limit

    reverted = 0
    if over or summary_over:
        lines = [t for _, _, t in over] + ([summary] if summary_over else [])
        limits = [bullet_limit] * len(over) + ([summary_char_limit] if summary_over else [])
        for _ in range(2):  # a second pass for anything the first left too long
            todo = [i for i, (l, lim) in enumerate(zip(lines, limits)) if len(l) > lim]
            if not todo:
                break
            for lim in sorted({limits[i] for i in todo}):  # bullets and summary separately
                group = [i for i in todo if limits[i] == lim]
                try:
                    fixed = _shorten_lines([lines[i] for i in group], lim)
                except Exception as exc:
                    log.warning("CVAgent: shortening pass failed (%s)", exc)
                    continue
                for i, new_text in zip(group, fixed):
                    if len(new_text) < len(lines[i]) and not _fabricated_figures(lines[i], new_text):
                        lines[i] = new_text
        if summary_over:
            new_summary = lines.pop()
            result["summary"] = new_summary if len(new_summary) <= summary_char_limit else summary_original
        for (ji, bi, _), new_text in zip(over, lines):
            block = job_blocks[ji]
            new_text = _trim_trailing_clause(new_text, bullet_limit)
            if len(new_text) <= bullet_limit:
                result["jobs"][ji]["bullets"][bi] = new_text
            else:
                result["jobs"][ji]["bullets"][bi] = block["_bullet_texts"][bi] \
                    if bi < len(block["_bullet_texts"]) else new_text
                reverted += 1

    skills = result.get("skills")
    if isinstance(skills, list):
        while len(skills) > 1 and len(", ".join(str(x) for x in skills)) > skills_limit:
            skills.pop()
    return reverted


# ── Claude call ────────────────────────────────────────────────────────────────

def _ask_claude(summary_word_ceiling_base, job_blocks, skills_items, role_title, role_desc, company,
                 industry, summary_tags: list, shorten_hint: str = "", length_scale: float = 1.0,
                 bullet_char_limit: int = 100, skills_char_limit: int = 500) -> dict:
    bullet_limit = max(40, int(bullet_char_limit * length_scale))
    jobs_payload = [
        {
            "context": b["context"],
            "bullet_slots": len(b["_bullet_texts"]),
            "max_characters_per_slot": [bullet_limit] * len(b["_bullet_texts"]),
            "fact_tags_per_bullet": b["_fact_tags"],
        }
        for b in job_blocks
    ]
    summary_word_ceiling = max(15, int(summary_word_ceiling_base * length_scale))
    skills_limit = max(150, int(skills_char_limit * length_scale))
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
would be fabrication, not tailoring. In particular never name the employer's product, domain,
regulation or industry (e.g. "catheter", "FDA 21 CFR 820", "medical devices", "LPBF") as something
Prateek has worked on unless his CV already says so, and never add a credential (Black Belt,
certified, chartered, ...) he doesn't list. Every attempt is checked word-by-word against his real
CV, and any job-ad term he has never used gets the whole CV rejected.

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
target. Every bullet is exactly ONE printed line on the page — a bullet that wraps onto a second line
pushes the CV onto two pages. For the summary: {summary_word_ceiling} words MAXIMUM (shorter is fine).
For each bullet: {bullet_limit} characters MAXIMUM including spaces (count as you write; shorter is
fine) — over-long bullets are cut back to the original wording automatically. For the whole skills
line: {skills_limit} characters MAXIMUM — drop the least relevant skills rather than exceed it.
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
        # Every master bullet fits on one printed line, so the longest one is the line's real
        # capacity; 95% of it leaves margin for wider characters. Same idea for the skills line
        # and summary: never longer than the master's own, which is known to fit.
        all_bullets = [t for b in job_blocks for t in b["_bullet_texts"]]
        line_capacity = int(max(len(t) for t in all_bullets) * 0.95) if all_bullets else 100
        skills_char_limit = len(", ".join(skills_items)) or 500
        summary_char_limit = len(summary_original) or 600
        original_standards = _standards(original_full_text + " " + ", ".join(skills_items))
        whole_cv_text = "\n".join(p.text for p in base_doc.paragraphs)
        jd_text = (f"{job.get('role_title', '')} {job.get('role_description', '')} "
                   f"{row.get('company_industry', '')}")
        # Bullets about a website/portfolio are personal facts, not achievements to reframe —
        # a rewrite once turned "portfolio site documenting 22 projects" into "managed a
        # 22-project portfolio". Those slots always keep their original wording.
        locked = {(ji, bi) for ji, b in enumerate(job_blocks) for bi, t in enumerate(b["_bullet_texts"])
                  if re.search(r"https?://|www\.|\.(dev|io|com|co\.uk)\b|portfolio", t, re.IGNORECASE)}

        facts = _extract_facts(job_blocks, summary_original)
        for block, proposed_job in zip(job_blocks, facts.get("jobs") or []):
            tags = (proposed_job.get("bullet_tags") if isinstance(proposed_job, dict) else None) or []
            # Fallback per-bullet if extraction came back short/malformed: use the raw bullet as its
            # own one-item "tag" rather than lose the fact entirely.
            block["_fact_tags"] = [
                tags[i] if i < len(tags) and tags[i] else [orig]
                for i, orig in enumerate(block["_bullet_texts"])
            ]
        for block in job_blocks:  # extraction returned fewer jobs than the CV has
            block.setdefault("_fact_tags", [[t] for t in block["_bullet_texts"]])
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
                bullet_char_limit=line_capacity,
                skills_char_limit=skills_char_limit,
            )
            if not _well_formed(result, job_blocks):
                log.warning("CVAgent: attempt %d returned a malformed rewrite, retrying", attempt)
                retry_hint = ("IMPORTANT: your previous answer was malformed. jobs[] must have exactly "
                              f"{len(job_blocks)} objects, each with a bullets[] list of exactly that "
                              "job's bullet_slots strings.")
                continue
            reverted = _enforce_lengths(
                result, job_blocks,
                bullet_limit=max(40, int(line_capacity * length_scale)),
                skills_limit=max(150, int(skills_char_limit * length_scale)),
                summary_char_limit=max(150, int(summary_char_limit * length_scale)),
                summary_original=summary_original,
            )
            if reverted:
                log.info("CVAgent: attempt %d — %d over-long bullet(s) kept as original wording", attempt, reverted)
            for ji, bi in locked:
                jobs_out = result.get("jobs") or []
                if ji < len(jobs_out) and bi < len(jobs_out[ji].get("bullets") or []):
                    jobs_out[ji]["bullets"][bi] = job_blocks[ji]["_bullet_texts"][bi]
            repaired = _repair_honesty(result, job_blocks, summary_original, whole_cv_text,
                                       jd_text, original_standards)
            if repaired:
                log.info("CVAgent: attempt %d — %d piece(s) put back to the real CV's wording "
                         "(job-ad terms, credentials or facts from another job)", attempt, repaired)

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

            new_text_all = _collect_text(attempt_doc, summary_idx, job_blocks)
            if skills_idx is not None:
                new_text_all += " " + attempt_doc.paragraphs[skills_idx].text
            borrowed = _jd_borrowed_terms(new_text_all, whole_cv_text, jd_text)
            credentials = _invented_credentials(new_text_all, whole_cv_text)
            if borrowed or credentials:
                log.warning("CVAgent: attempt %d presented job-ad terms / credentials as his own: %s, retrying",
                            attempt, sorted(borrowed | credentials))
                retry_hint = (
                    f"IMPORTANT: your previous attempt used these words, which come from the job "
                    f"description but appear NOWHERE in Prateek's real CV: {sorted(borrowed | credentials)}. "
                    f"That presents the employer's domain or a credential as his own experience, which "
                    f"is fabrication. Describe his real work in his CV's own terms; the job ad's wording "
                    f"may only be used for skills he genuinely has under a different name."
                )
                continue

            moved = _misattributed_bullets(job_blocks, result, jd_text, whole_cv_text)
            if moved:
                log.warning("CVAgent: attempt %d put facts under the wrong job: %s, retrying", attempt,
                            [(job_ctx, terms) for job_ctx, _, terms in moved])
                retry_hint = (
                    "IMPORTANT: your previous attempt placed facts under the wrong employer: "
                    + "; ".join(f'"{text}" under {job_ctx} uses {terms}, which belong to a different job'
                                for job_ctx, text, terms in moved)
                    + ". Each job's bullets may only use that job's own fact tags."
                )
                continue

            invented_standards = _standards(new_text_all) - original_standards
            if invented_standards:
                log.warning("CVAgent: attempt %d claimed standards not in the real CV: %s, retrying",
                            attempt, sorted(invented_standards))
                retry_hint = (
                    f"IMPORTANT: your previous attempt claimed these standards/certifications, which "
                    f"appear nowhere in Prateek's real CV: {sorted(invented_standards)}. Never copy a "
                    f"standard or certification from the job description unless the CV already has it."
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
