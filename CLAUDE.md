# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Automated job outreach system for a UK **and Ireland** additive manufacturing job search. It reads a Google Sheet of target contacts, researches each company's website, writes personalised cold emails via Claude, and sends them through 3 Gmail accounts. Each contact receives up to 3 emails (initial + 2 follow-ups) spaced 5 working days apart, tracked back in the sheet.

UK and Ireland leads live in the same 3 sender tabs (not separate sheets/projects) — a `Country` column per row drives which country-specific config applies (location filters, ATS location matching, bank holidays, role-matching phrasing). This was a deliberate design choice over cloning a second project: both countries share the same 3 Gmail accounts, so one pipeline with one set of daily send limits avoids doubling send volume per account.

## Commands

```bash
# First-time setup — OAuth for Gmail + Sheets per account
python setup_auth.py

# Install cron jobs (fires every 15 min Mon–Fri; script self-exits outside BST windows)
bash setup_cron.sh

# Run the full pipeline manually (respects send windows — exits silently if outside)
python email_runner.py

# Check for replies/OOO/departures manually (active rows only)
python reply_checker.py

# Same, but a full sweep of every row with a thread — including resolved ones —
# in case a reply or departure notice was missed while a row was still active
python reply_checker.py --full

# Sort all sheets by next followup date
python sheet_sort.py
```

Logs go to `logs/email_runner.log`, `logs/reply_checker.log`.

## Architecture

```
email_runner.py          — cron entry point; orchestrates one send cycle per run
reply_checker.py         — cron entry point; polls Gmail for replies, classifies each as real reply / OOO / left-company in one pass, updates sheet; `--full` widens the scan to every row with a thread, not just active ones
sheet_sort.py            — sorts each sender's sheet by next followup date
log_writer.py            — writes send history to an email_logs sheet
config/settings.py       — single source of truth: senders, sheet columns, limits, windows
agents/
  sheet_agent.py         — reads eligible rows from Google Sheets; writes status back
  research_agent.py      — crawls company sites, detects ATS platforms, selects matching role
  email_writer.py        — generates HTML emails via Claude API (Sonnet for initial, Haiku for role selection)
  gmail_agent.py         — sends multipart/mixed email (HTML + plain + CV attachment); OOO detection
  reply_classifier.py    — classify_reply(): single LLM call decides real reply / OOO / left-company for a gathered reply body
```

### Flow per email cycle

1. `email_runner.py` checks the UK BST send window and skips if outside it
2. For each sender (3 Gmail accounts, run in parallel threads): `SheetAgent` reads the sheet, selects one eligible row (followups first, fresh contacts as fallback), respects daily limits and tier quotas
3. For fresh emails: `research_agent.find_matching_role(company_website, company_name, country=row["country"])` fetches the company homepage, detects the ATS platform (Greenhouse, Lever, Workable, Ashby, BambooHR, Workday, etc.), pulls job listings via API filtered to the row's country, and asks Claude Sonnet to pick the best-matching role in that country
4. `email_writer.write_email()` generates the email with the appropriate sequence step (initial / followup 1 / followup 2), then `GmailAgent.send_email()` sends it
5. `SheetAgent.mark_sent()` re-fetches the live row number before writing (guards against sort shifting rows between read and write)
6. After all senders complete: `sheet_sort`, `reply_checker`, `expire_completed_sequences` (per sender — marks contacts `not interested` once their post-final-send checkpoint passes with no reply), and `log_writer` run sequentially

### Key configuration (`config/settings.py`)

- `SENDERS` — list of `{name, email, sheet_name, token_file, cv_path}` for each Gmail account
- `COLUMNS` — maps logical field names to actual sheet header strings (includes `"country": "Country"`)
- `DAILY_LIMIT_WEEKDAY/FRIDAY`, `DAILY_PER_TIER`, `MAX_SEQUENCE` — campaign rate controls (shared across both countries — not per-country)
- `MORNING_WINDOW`, `FRIDAY_WINDOW` — send windows in UK local time (pytz handles BST/GMT automatically); shared by UK and Ireland rows since the two timezones align
- `CV_CONTEXT`, `CANDIDATE_NAME` etc. — candidate profile injected into all AI prompts
- `COUNTRY_CONFIG` / `get_country_config(country)` — per-country location keywords, ATS location-filter codes, role-matching phrasing, and CV location line; row's `Country` column (defaults to `"UK"` when blank) selects which entry applies. `get_cv_context(country)` builds the country-specific CV_CONTEXT used in role-matching prompts.

### Sheet status state machine

`status` column values (all lowercase): blank → `followup initiated` → `bounced` / `discussion in progress` / `no longer with company` / `not interested`  
`reply_status`: blank → `reply received`

`followup initiated` is reused for the post-final-send waiting period: after the last (3rd) send, `mark_sent()` still schedules a `next_followup_date` (5 working days out) and leaves status as `followup initiated` — it just isn't picked up for another send since `sequence_step >= MAX_SEQUENCE`. Once that checkpoint date passes with still no reply, `email_runner.py`'s post-cycle `expire_completed_sequences()` step (one call per sender, after `reply_checker` runs) flips the row to `not interested`.

### ATS detection in `research_agent.py`

`_try_ats_api()` scans raw homepage HTML for ATS indicator URLs, then calls the matching platform API (not scraping — structured JSON). Falls back to scraping if the API returns nothing. Workday uses RSS feed. The detection runs twice: on the homepage and again on the careers page (ATS links often only appear on the careers page). Every ATS handler takes a `country` param and filters/labels listings using `get_country_config(country)` (location keywords, Workday location codes, ConnectID/Gaia country filters) — see `COUNTRY_CONFIG` in `config/settings.py`.

### Per-country bank holidays in `email_runner.py`

`_BANK_HOLIDAYS` is `{"UK": set(...), "Ireland": set(...)}`, loaded once per run. UK uses the England & Wales gov.uk API (`config/uk_bank_holidays.json` cache); Ireland uses the Nager.Date public API (`config/ie_bank_holidays.json` cache, fetched for the current + next year since it's per-year). `get_run_mode()` only checks weekday/time window — it does NOT skip the whole run for a holiday, since UK and Ireland rows share the same cron cycle and a holiday in one country shouldn't halt the other. Instead, `process_sender()` filters each row's eligibility by its own `country` via `_is_bank_holiday(today, row["country"])` before selection, and `next_followup_date(send_date, country)` uses that country's calendar for working-day math.

## Environment

Requires `.env` at the project root:
```
ANTHROPIC_API_KEY=...
EXA_API_KEY=...          # optional — used for LinkedIn profile lookups in email_writer
```

OAuth tokens are per-account at `config/tokens/token_<account>.json`. Run `setup_auth.py` once per account; tokens auto-refresh thereafter. `config/credentials.json` is the Google OAuth client credentials file.

## Important invariants

- `SheetAgent.__init__` always uses `SENDERS[0]`'s credentials to open the spreadsheet (owner access), regardless of which sender is being processed — only the worksheet tab differs per sender
- `mark_sent()` always re-fetches the live row number for the recipient email before writing, because `sort_all_sheets` may run between row selection and the write-back
- Lock files (`logs/*.lock`) prevent concurrent instances of the same script; a running instance causes the new one to `sys.exit(0)` silently
- The cron runs in IST (machine timezone) but all send-window logic uses UK BST via pytz; the script self-exits if called outside the active window
