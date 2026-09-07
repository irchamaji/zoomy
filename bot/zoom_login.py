#!/usr/bin/env python3
"""Capture a signed-in zoom.us session for the bot to reuse.

Some hosts require every participant to be signed in with a Zoom account. The
recorder joins through the Zoom web client, so all it needs is a logged-in
browser session — this script opens a real browser window, lets *you* log in by
hand, and saves the resulting cookies as a Playwright storage_state file that
`recorder.py` loads into every recording session.

Run it on a machine with a screen (your laptop), not inside the container:

    python3 bot/zoom_login.py                       # → secrets/zoom_state.json
    python3 bot/zoom_login.py -o /tmp/zoom.json     # somewhere else

then copy the file to the server's `secrets/` directory and restart the stack.

The login is deliberately manual: Zoom answers scripted credential entry with
2FA prompts, device-verification mail and CAPTCHAs, and this way your password
never has to live in .env or pass through this script at all.

The output file is a live login — treat it like a password. It is written
0600 and `secrets/` is gitignored.
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright is not installed. Run: pip install playwright && playwright install chromium")

SIGNIN_URL = "https://zoom.us/signin"
VERIFY_URL = "https://zoom.us/profile"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "secrets" / "zoom_state.json"

# Mirrors the flags recorder.py launches Chrome with, so the session Zoom issues
# here belongs to the same-looking browser that will later use it.
BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,900",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Save a signed-in Zoom session for the recorder bot.")
    ap.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"where to write the session file (default: {DEFAULT_OUTPUT})",
    )
    ap.add_argument(
        "--url", default=SIGNIN_URL,
        help=f"sign-in page to open (default: {SIGNIN_URL}; use your org's SSO URL if needed)",
    )
    args = ap.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Opening {args.url} …")
    print()
    print("  1. Sign in as the account the bot should join meetings as.")
    print("  2. Complete any 2FA / device verification Zoom asks for.")
    print("  3. Leave the browser open and come back here.")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=BROWSER_ARGS)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        try:
            input("Press Enter here once you are logged in (Ctrl-C to abort)… ")
        except KeyboardInterrupt:
            print("\nAborted — nothing saved.")
            browser.close()
            return 1
        except EOFError:
            # No terminal on stdin — e.g. launched by a script or an agent.
            # There is nobody to do the logging in, so stop rather than save
            # the empty session that would result.
            print("\nNo interactive terminal — run this yourself in a shell.")
            browser.close()
            return 1

        # Confirm the session is real rather than trusting the prompt: an
        # unauthenticated hit on the profile page bounces back to sign-in.
        print(f"Verifying against {VERIFY_URL} …")
        try:
            page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
        except Exception as e:
            print(f"Could not load the profile page: {e}")
            browser.close()
            return 1

        if "signin" in page.url or "login" in page.url:
            print(f"Still signed out (landed on {page.url}). Nothing saved — try again.")
            browser.close()
            return 1

        state = ctx.storage_state()
        browser.close()

    cookies = [c for c in state.get("cookies", []) if "zoom.us" in c.get("domain", "")]
    if not cookies:
        print("Signed in, but no zoom.us cookies were captured. Nothing saved.")
        return 1

    args.output.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.chmod(args.output, 0o600)

    print()
    print(f"Saved {len(cookies)} zoom.us cookies → {args.output}")
    print("This file is a live login — do not commit or share it.")
    print()
    print("Next: copy it to the server's secrets/ directory and rebuild:")
    print("  docker compose up -d --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
