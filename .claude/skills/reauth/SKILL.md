---
name: reauth
description: Re-authenticate a specific Gmail sender account when its OAuth token has expired or been revoked (seen as "invalid_grant" errors in logs/email_runner.log or logs/reply_checker.log). Only triggers on explicit user request — has side effects (overwrites a token file, opens a browser consent flow).
disable-model-invocation: true
---

Re-authenticate one Gmail sender account for AM_UK: $ARGUMENTS

1. If no account/email was given in `$ARGUMENTS`, ask which sender from `config/settings.py`'s `SENDERS` list needs re-auth (or grep the logs for the account named in the most recent `invalid_grant` error).
2. Confirm with the user before touching anything — this overwrites a real credential file and will open a browser OAuth consent screen.
3. Locate that sender's token file at `config/tokens/token_<account>.json` and move it aside (don't delete outright) so `setup_auth.py` is forced to run a fresh OAuth flow for that account instead of silently reusing the stale token.
4. Run `python setup_auth.py`, following its prompts for the affected account only.
5. Confirm the new token file was written, then verify with a quick manual check (e.g. re-run `reply_checker.py` or `test_row.py` for that sender) that the `invalid_grant` error is gone.
