---
name: run-am-uk
description: Run, test, or manually trigger the AM_UK email outreach pipeline. Use when asked to run the pipeline, test a row, sort sheets, check replies, verify a change works, or confirm the system is healthy.
---

This is a cron-based Python pipeline, not a long-running server. The driver is `smoke.sh` — a smoke script that exercises the four main scripts end-to-end without sending any emails. All paths below are relative to the project root (`AM_UK/`).

## Prerequisites

All dependencies live in this project's own virtualenv at `AM_UK/.venv`, not the system Python. Activate it (from the project root) before running anything manually:
```bash
source .venv/bin/activate
```
The cron job (`setup_cron.sh`) and `smoke.sh` already call `.venv/bin/python3` directly, so they don't need the venv activated first. OAuth tokens must exist at `config/tokens/token_*.json` — if missing, run `python3 setup_auth.py` (inside the activated venv) to re-authenticate.

Playwright (used by `research_agent.py`) requires a one-time Chromium download, into the same venv:
```bash
source .venv/bin/activate
python3 -m playwright install chromium
```

## Run (agent path)

```bash
# Fast check (~30s) — window check + sheet sort + full pipeline dry-run of row 2
bash .claude/skills/run-am-uk/smoke.sh

# Full check (~2min) — adds live Gmail reply scan across all 3 accounts
bash .claude/skills/run-am-uk/smoke.sh --full
```

The dry-run shows the researched company, the selected role, and the full generated email body. Nothing is written to the sheet or sent.

To test a specific row or account:
```bash
# Dry-run row 5 on hksahni0's sheet
python3 test_row.py 5 hksahni0

# Dry-run row 3 on prateek.sahni94's sheet
python3 test_row.py 3 prateek.sahni94

# Sheet names: hksahni0 | prateek.sahni94 | sprateek11294
```

## Run individual scripts manually

```bash
# Trigger a full send cycle (only sends if current time is inside BST window)
python3 email_runner.py

# Check for replies / OOO / bounces across all 3 Gmail accounts
python3 reply_checker.py

# Sort all 3 sheets by next followup date
python3 sheet_sort.py

# Update the email_logs sheet
python3 log_writer.py
```

## Live send (destructive — sends a real email)

```bash
# Send to row 5, hksahni0 account, and update the sheet
python3 test_row.py 5 hksahni0 --send
```

## Check logs

```bash
tail -50 logs/email_runner.log
tail -50 logs/reply_checker.log
```

## Gotchas

- **`email_runner.py` exits silently outside the BST send window.** Mon–Thu 08:30–12:30 and 15:00–18:00 BST; Friday 08:30–12:30. Outside those hours the log says `Outside send window (... BST) — skipping` and exits 0. This is correct behaviour.

- **Lock files in `logs/`** — if `logs/email_runner.lock` or `logs/reply_checker.lock` exists, the script exits immediately without logging. Check with `flock -n logs/email_runner.lock true && echo FREE || echo HELD`. A FREE result means the file is a harmless leftover; delete it. A HELD result means the process is still running.

- **Harmless warnings on every run** — three FutureWarning lines from google-auth (Python 3.9 is past EOL) and a NotOpenSSLWarning from urllib3 (system LibreSSL vs OpenSSL). None affect behaviour. The smoke script filters most of them.

- **`test_row.py` imports `email_runner` at module level**, which acquires `logs/email_runner.lock` as a side effect. Running `test_row.py` while the cron is mid-run will silently fail (the cron holds the lock, `test_row.py` gets IOError and exits 0).

- **`test_row.py` dry-run still calls the Claude API** to write the email. If `ANTHROPIC_API_KEY` in `.env` is missing or expired, it will fail here.

- **`setup_cron.sh` uses `$(which python3)`** — if it's ever re-run without the venv activated first, it will reinstall the cron job pointing at whatever `python3` is on PATH (likely the system Python again, which lacks these dependencies). Always `source .venv/bin/activate` before re-running `setup_cron.sh`.
