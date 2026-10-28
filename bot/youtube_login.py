#!/usr/bin/env python3
"""Authorize the bot to upload to your YouTube channel (one-time, on your laptop).

You need an OAuth client of type "Desktop app" from the Google Cloud Console
(YouTube Data API v3 enabled). Download its JSON, then:

    pip install google-auth-oauthlib
    python3 bot/youtube_login.py client_secret.json          # → secrets/youtube_token.json
    python3 bot/youtube_login.py client_secret.json -o /tmp/t.json

A browser window opens; sign in with the Google account that owns the channel and
approve. The script saves the resulting refresh token, which `youtube.py` uses to
upload without ever asking again. Copy the file to the server's `secrets/`
directory and rebuild the stack.

Set the OAuth consent screen to "In production" first: while it is "Testing",
Google expires the refresh token after 7 days.

The output file is a live credential — treat it like a password. It is written
0600 and `secrets/` is gitignored.
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit("google-auth-oauthlib is not installed. Run: pip install google-auth-oauthlib")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "secrets" / "youtube_token.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("client_secret", type=Path, help="OAuth client JSON downloaded from Google Cloud Console")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.client_secret.is_file():
        sys.exit(f"Client secret file not found: {args.client_secret}")

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), SCOPES)
    # offline + consent forces a refresh token even if this account authorized the app before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        sys.exit("Google returned no refresh token. Revoke the app at myaccount.google.com/permissions and retry.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    os.chmod(args.output, 0o600)
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
