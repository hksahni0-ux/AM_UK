import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent

# ── Google Sheets ─────────────────────────────────────────────────────────────
SPREADSHEET_ID = "1xC0j0rF_Tovch5omSzFIN_fTuA2i48T00LtdekBSzNQ"

# ── Sender accounts ───────────────────────────────────────────────────────────
# cv_path: path to CV PDF (all share the same CV; set individually if different)
SENDERS = [
    {
        "name": "Prateek Sahni",
        "email": "hksahni0@gmail.com",
        "sheet_name": "hksahni0",
        "token_file": str(BASE_DIR / "config" / "tokens" / "token_hksahni0.json"),
        "cv_path": str(BASE_DIR / "cv" / "_PRATEEK CV_2026.pdf"),
    },
    {
        "name": "Prateek Sahni",
        "email": "prateek.sahni94@gmail.com",
        "sheet_name": "prateek.sahni94",
        "token_file": str(BASE_DIR / "config" / "tokens" / "token_prateek_sahni94.json"),
        "cv_path": str(BASE_DIR / "cv" / "_PRATEEK CV_2026.pdf"),
    },
    {
        "name": "Prateek Sahni",
        "email": "sprateek11294@gmail.com",
        "sheet_name": "sprateek11294",
        "token_file": str(BASE_DIR / "config" / "tokens" / "token_sprateek11294.json"),
        "cv_path": str(BASE_DIR / "cv" / "_PRATEEK CV_2026.pdf"),
    },
]

# ── Sheet column names — matched to actual sheet headers ──────────────────────
COLUMNS = {
    "first_name":         "First name",
    "last_name":          "Last name",
    "recipient_email":    "Email",
    "company_name":       "Company name",
    "recipient_job_title":"Job title",        # the contact's job title
    "company_website":    "Company website",
    "company_industry":   "Company industry",
    "status":             "Status",
    "reply_status":       "Reply Status",
    "next_followup_date": "Next Followup",
    "sequence_step":      "Sequence Step",
    "last_action_date":   "Last Action Date",
    "comments":           "Comments",
    "role_applied":        "Role Applied",
    "person_linkedin_url": "Person LinkedIn URL",
    "tier":               "Tier",
    "category":           "Category",
    "thread_id":          "Thread ID",
    "country":            "Country",
}

# ── Status values (used in sheet — all lowercase) ─────────────────────────────
STATUS_BLANK                = ""
STATUS_FOLLOWUP_INITIATED   = "followup initiated"
STATUS_DISCUSSION           = "discussion in progress"
STATUS_NOT_INTERESTED       = "not interested"
STATUS_BOUNCED              = "bounced"
STATUS_NO_LONGER_WITH_COMPANY = "no longer with company"
REPLY_STATUS_RECEIVED       = "reply received"

# ── Campaign settings ─────────────────────────────────────────────────────────
DAILY_LIMIT_WEEKDAY  = 25      # Mon–Thu: emails per sender per day (10:00–16:00 BST window)
DAILY_LIMIT_FRIDAY   = 25      # Friday: emails per sender per day (caps at ~16 — morning window only)
DAILY_PER_TIER       = 2       # target fresh emails per tier per sender (Mon–Thu)
FOLLOWUP_GAP_WORKING_DAYS = 5  # working days from one send to the next (Mon → Mon next week)
MAX_SEQUENCE         = 3       # total emails per recipient (1 initial + 2 followups)

# ── Temporary send-priority override (2026-09-02) ──────────────────────────────
# While True: UK fresh (first-touch) sends are held back per sender, in favour of
# that sender's UK followups and all Ireland activity (fresh + followups) —
# clearing the existing UK followup backlog and standing up the Ireland campaign
# before resuming UK top-of-funnel outreach. Evaluated per day, not overall:
# SheetAgent.get_work_status() only counts followups actually due *today* — a
# followup scheduled for a future date doesn't hold the pause, so a sender with
# nothing actionable today still sends UK fresh rather than sitting idle. Set to
# False to lift the pause immediately regardless of what's still queued.
PAUSE_UK_FRESH_SENDS = True

TIMEZONE             = "Europe/London"

# ── Send windows (UK local time — pytz handles BST/GMT automatically) ────────
# Mon–Thu: single window 10:00–16:00
MORNING_WINDOW   = ((10, 0), (16, 1))    # 10:00–16:00 inclusive
# Friday: followups only, morning window
FRIDAY_WINDOW    = ((8, 30), (12, 31))   # 08:30–12:30 inclusive

# ── API keys ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
EXA_API_KEY             = os.getenv("EXA_API_KEY", "")
GOOGLE_CREDENTIALS_FILE = str(BASE_DIR / "config" / "credentials.json")

# ── LLM provider switch ────────────────────────────────────────────────────────
# "anthropic" | "nvidia" — flip back to "anthropic" in .env once Anthropic credits
# are topped up; no code changes needed, every call site reads this.
LLM_PROVIDER      = os.getenv("LLM_PROVIDER", "anthropic")
NVIDIA_API_KEY    = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_API_BASE   = "https://integrate.api.nvidia.com/v1"

# Model used per task, per provider — keeps call sites provider-agnostic.
LLM_MODELS = {
    "anthropic": {
        "email_write":      "claude-sonnet-4-6",
        "cv_extract_facts": "claude-sonnet-4-6",
        "cv_write":         "claude-sonnet-4-6",
        "role_match":       "claude-sonnet-4-6",
        "link_pick":        "claude-haiku-4-5-20251001",
        "ooo_extract":      "claude-haiku-4-5-20251001",
        "left_company_check": "claude-haiku-4-5-20251001",
    },
    "nvidia": {
        "email_write":      "nvidia/nemotron-3-ultra-550b-a55b",
        "cv_extract_facts": "nvidia/nemotron-3-ultra-550b-a55b",
        "cv_write":         "nvidia/nemotron-3-ultra-550b-a55b",
        "role_match":       "nvidia/nemotron-3-super-120b-a12b",
        "link_pick":        "nvidia/nemotron-3-nano-30b-a3b",
        "ooo_extract":      "nvidia/nemotron-3-nano-30b-a3b",
        # nano was too liberal on this nuanced judgment call (flagged plain declines
        # as "left the company") — use the larger model, this task runs rarely.
        "left_company_check": "nvidia/nemotron-3-super-120b-a12b",
    },
}

# OAuth scopes (Gmail send+read + Sheets + Drive-file for docx→PDF conversion)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ── Candidate signature details (same across all 3 sender accounts) ───────────
CANDIDATE_NAME      = "Prateek Sahni, MEng"
CANDIDATE_PHONE     = "+91 9716922930"
CANDIDATE_LINKEDIN  = "linkedin.com/in/prateek-sahni-meng"

# ── CV tailoring ───────────────────────────────────────────────────────────────
CV_MASTER_DOCX  = str(BASE_DIR / "cv" / "_PRATEEK CV_2026.docx")
CV_GENERATED_DIR = str(BASE_DIR / "cv" / "generated")

# ── Country-specific targeting ─────────────────────────────────────────────────
# The "Country" sheet column (defaults to "UK" when blank) selects which of these
# configs applies per-row — location keyword lists, ATS location filters, and
# role-matching phrasing all branch on it so one pipeline can run both campaigns.
DEFAULT_COUNTRY = "UK"

COUNTRY_CONFIG = {
    "UK": {
        "label": "UK",
        "country_full_name": "United Kingdom",
        "location_keywords": [
            "borough", "shire", "london", "manchester", "cambridge", "oxford",
            "bristol", "farnborough", "sheffield", "edinburgh", "birmingham",
            "reading", "guildford", "swindon", "coventry", "leeds", "nottingham",
            "glasgow", "derby", "york", "bath", "brighton", "portsmouth",
            "southampton", "exeter", "newcastle", "remote", "hybrid", "uk",
        ],
        "location_suffix": "UK",
        "workday_codes": ["GBR", "UK", "GB"],
        "connectid_country": "United Kingdom",
        "gaia_country_code": "GB",
        "role_match_phrase": "UK",
        "example_cities": "Farnborough, London, Remote",
        "research_intro": "additive and advanced manufacturing companies in the UK",
        "cv_location": "UK (willing to relocate within UK)",
    },
    "Ireland": {
        "label": "Ireland",
        "country_full_name": "Ireland",
        "location_keywords": [
            "dublin", "cork", "galway", "limerick", "waterford", "kilkenny",
            "athlone", "sligo", "shannon", "drogheda", "dundalk", "navan",
            "tullamore", "mullingar", "naas", "wexford", "kildare",
            "ireland", "irl", "remote", "hybrid",
        ],
        "location_suffix": "Ireland",
        "workday_codes": ["IRL", "IE", "Ireland"],
        "connectid_country": "Ireland",
        "gaia_country_code": "IE",
        "role_match_phrase": "Ireland",
        "example_cities": "Dublin, Cork, Remote",
        "research_intro": "additive and advanced manufacturing companies in Ireland",
        "cv_location": "Ireland (willing to relocate within Ireland)",
    },
}


def get_country_config(country: str) -> dict:
    return COUNTRY_CONFIG.get(country, COUNTRY_CONFIG[DEFAULT_COUNTRY])


# ── CV & candidate context ────────────────────────────────────────────────────
# Used by the AI to identify relevant roles and write emails.
CV_CONTEXT_TEMPLATE = """
Candidate: Prateek Sahni
Field: Additive Manufacturing / Advanced Manufacturing

Core expertise:
- Additive manufacturing processes: FDM, SLA, SLS, DMLS/LPBF, EBM, binder jetting
- Process engineering, process development, and optimisation
- Materials characterisation (metals, polymers, composites)
- Design for Additive Manufacturing (DfAM)
- Production scale-up, quality management, and troubleshooting
- R&D in advanced manufacturing technologies
- CAD/CAM, simulation tools, post-processing

Target roles (in order of preference):
1. Additive Manufacturing Engineer
2. Process Development Engineer
3. Process Engineer (manufacturing focus)
4. Manufacturing Engineer (additive/advanced)
5. R&D Engineer (manufacturing)
6. Any suitable technical engineering role

Location: {location}
"""


def get_cv_context(country: str = DEFAULT_COUNTRY) -> str:
    return CV_CONTEXT_TEMPLATE.format(location=get_country_config(country)["cv_location"])


# Backward-compatible default (UK) — kept for the CV-extraction-failure fallback
# in email_writer.py, which doesn't need per-country precision.
CV_CONTEXT = get_cv_context(DEFAULT_COUNTRY)
