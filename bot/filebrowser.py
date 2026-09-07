import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

FILEBROWSER_URL = os.environ.get("FILEBROWSER_URL", "").rstrip("/")
FILEBROWSER_API_TOKEN = os.environ.get("FILEBROWSER_API_TOKEN", "")
FILEBROWSER_SOURCE_NAME = os.environ.get("FILEBROWSER_SOURCE_NAME", "recordings")
FILEBROWSER_SHARE_EXPIRY_DAYS = os.environ.get("FILEBROWSER_SHARE_EXPIRY_DAYS", "90")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=FILEBROWSER_URL,
            headers={"Authorization": f"Bearer {FILEBROWSER_API_TOKEN}"},
            timeout=15.0,
        )
    return _client


async def create_share(relative_path: str) -> tuple[str, datetime]:
    """Create a public share link for a file under the 'recordings' source.

    relative_path is relative to RECORDINGS_DIR, e.g.
    "MyMeeting_20260709/MyMeeting.mp4".

    Returns (share_url, expire_at) — the public share URL and its expiry
    as a timezone-aware UTC datetime, taken from the server's response.
    """
    if not is_configured():
        raise ValueError("FILEBROWSER_URL / FILEBROWSER_API_TOKEN not set.")

    path = "/" + relative_path.lstrip("/")
    client = _get_client()
    resp = await client.post(
        "/api/share",
        json={
            "path": path,
            "source": FILEBROWSER_SOURCE_NAME,
            "expires": FILEBROWSER_SHARE_EXPIRY_DAYS,
            "unit": "days",
            "shareType": "normal",
            "keepAfterExpiration": True,
        },
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"FileBrowser share creation failed ({resp.status_code}): {detail}")

    data = resp.json()
    share_url = data.get("shareURL")
    expire_ts = data.get("expire")
    if not share_url or not expire_ts:
        raise RuntimeError(f"FileBrowser response missing shareURL/expire: {data}")
    expire_at = datetime.fromtimestamp(expire_ts, tz=timezone.utc)
    logger.info("Created share link for %s -> %s (expires %s)", relative_path, share_url, expire_at)
    return share_url, expire_at


def is_configured() -> bool:
    return bool(FILEBROWSER_URL and FILEBROWSER_API_TOKEN)
