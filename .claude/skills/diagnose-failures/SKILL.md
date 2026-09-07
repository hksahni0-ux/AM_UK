---
name: diagnose-failures
description: Diagnose why the AM_UK email outreach pipeline isn't sending or reply-checking correctly. Scans logs/email_runner.log and logs/reply_checker.log for errors, checks for stale lock files, and identifies which sender account is affected. Use when a cron run seems to have failed silently, no emails went out, or the user asks "why isn't this working".
---

Diagnose failures in the email outreach pipeline:

1. Tail the last ~200 lines of `logs/email_runner.log` and `logs/reply_checker.log` and grep for `ERROR`, `Traceback`, `Exception`.
2. Check for two known recurring failure patterns and call them out explicitly if found:
   - `ModuleNotFoundError: No module named 'babel'` (or any other module) — a dependency listed in `requirements.txt` isn't actually installed in the runtime environment. Suggest `pip install -r requirements.txt` in the environment the cron job actually uses (check `setup_cron.sh` for the interpreter path).
   - `invalid_grant: Token has been expired or revoked` — an OAuth token for one of the sender accounts in `config/settings.py`'s `SENDERS` list has expired. Identify which sender/account from the surrounding log context, then point the user to the `reauth` skill for that account.
3. Check `logs/email_runner.lock` and `logs/reply_checker.lock` — if either exists, probe whether it is actually held by a live process using `flock -n logs/email_runner.lock true 2>/dev/null && echo FREE || echo HELD` (and the same for reply_checker.lock). If `flock -n` returns FREE the file exists but no process holds the lock — it is a cosmetic leftover and does NOT block future runs (the next cron will acquire it normally). If `flock -n` returns HELD, a process is actively running; report this as expected if `ps aux | grep -E 'email_runner|reply_checker'` shows a matching process, or as a genuine stuck lock if no process is found. Only flag as a problem if HELD with no matching process.
4. Summarize: what failed, which sender account(s) are affected, and the concrete next step (reinstall a dependency, re-run `reauth` for an account, or clear a stale lock).

Don't take corrective action (deleting locks, editing code) without confirming with the user first — this pipeline sends real emails from real accounts on a live cron schedule.
