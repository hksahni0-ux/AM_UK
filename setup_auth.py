#!/usr/bin/env python3
"""
setup_auth.py — one-time OAuth setup for all 3 Gmail accounts.
Run this once before using the email system:

    python3 setup_auth.py

A browser window will open for each account. Sign in and grant permissions.
Tokens are saved to config/tokens/ and auto-refreshed on subsequent runs.
"""

import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

sys.path.insert(0, str(Path(__file__).parent))
from config.settings import SENDERS, GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES


def authenticate_account(sender: dict):
    token_file = sender["token_file"]
    email = sender["email"]
    name = sender["name"]

    Path(token_file).parent.mkdir(parents=True, exist_ok=True)

    # Skip if already authenticated
    if Path(token_file).exists():
        try:
            creds = Credentials.from_authorized_user_file(token_file, GOOGLE_SCOPES)
            if creds.valid:
                print(f"  ✓ {email} — already authenticated, skipping")
                return
        except Exception:
            pass  # re-authenticate

    print(f"\n{'='*60}")
    print(f"  Authenticating: {name} ({email})")
    print(f"{'='*60}")
    print(f"  A browser window will open. Sign in as: {email}")
    input("  Press Enter to continue...")

    if not Path(GOOGLE_CREDENTIALS_FILE).exists():
        print(f"\n  ERROR: credentials.json not found at:")
        print(f"  {GOOGLE_CREDENTIALS_FILE}")
        print(f"\n  Steps to get it:")
        print(f"  1. Go to console.cloud.google.com")
        print(f"  2. Create/select a project")
        print(f"  3. Enable Gmail API and Google Sheets API")
        print(f"  4. APIs & Services → Credentials → Create OAuth Client ID")
        print(f"  5. Application type: Desktop App")
        print(f"  6. Download → rename to credentials.json → place in config/")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent", login_hint=email)

    # Save token
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GOOGLE_SCOPES),
    }
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"  ✓ Token saved to {token_file}")


def main():
    print("\nAM_UK Email System — OAuth Setup")
    print("="*60)
    print(f"Setting up {len(SENDERS)} accounts...\n")

    for sender in SENDERS:
        authenticate_account(sender)

    print("\n" + "="*60)
    print("  All accounts authenticated!")
    print("  You can now run:")
    print("  python3 email_runner.py   — send one batch manually")
    print("  python3 reply_checker.py  — check for replies manually")
    print("  bash setup_cron.sh        — install cron jobs")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
