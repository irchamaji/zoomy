"""Upload finished recordings to the owner's YouTube channel and expire local copies.

Flow (driven from bot.py):
  1. A recording finishes -> upload_recording() pushes the mp4 to YouTube.
  2. On success a `.youtube.json` marker is written into the recording folder.
  3. cleanup_expired() deletes the *whole folder* (mp4, txt, srt, metadata, …)
     once the marker is older than YOUTUBE_RETENTION_DAYS and YouTube still
     reports the video as processed.

A folder without a marker — upload failed, rejected, never attempted — is never
deleted. The marker is the only thing that makes a folder eligible.

Auth is the refresh token that bot/youtube_login.py writes to YOUTUBE_TOKEN_PATH.
The google-* libraries are imported lazily so the bot still starts without them.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

YOUTUBE_TOKEN_PATH = Path(os.environ.get("YOUTUBE_TOKEN_PATH", "/secrets/youtube_token.json"))
YOUTUBE_PRIVACY = os.environ.get("YOUTUBE_PRIVACY", "unlisted").strip().lower()
YOUTUBE_CATEGORY_ID = os.environ.get("YOUTUBE_CATEGORY_ID", "22")  # 22 = People & Blogs
YOUTUBE_RETENTION_DAYS = int(os.environ.get("YOUTUBE_RETENTION_DAYS", "7"))

MARKER_NAME = ".youtube.json"

# How long upload_recording() waits for YouTube to finish processing before it
# gives up and leaves the marker as "processing" (cleanup re-checks later).
POLL_INTERVAL_SECS = 30
POLL_TIMEOUT_SECS = 20 * 60

_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_CHUNK_RETRIES = 6
_RETRY_STATUSES = {500, 502, 503, 504}

# One upload at a time: they are long, and parallel ones would just split the
# uplink so every one of them takes longer to finish.
_upload_lock = asyncio.Lock()

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class YouTubeError(RuntimeError):
    pass


def is_configured() -> bool:
    return YOUTUBE_TOKEN_PATH.is_file()


def _privacy() -> str:
    return YOUTUBE_PRIVACY if YOUTUBE_PRIVACY in {"private", "unlisted", "public"} else "unlisted"


def _service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_PATH), _SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _safe_title(name: str) -> str:
    # The API rejects < and > in titles; 100 chars is the hard cap.
    return re.sub(r"[<>]", "", name).strip()[:100] or "Zoomy recording"


def upload_file(path: Path, title: str, description: str = "") -> tuple[str, str]:
    """Blocking resumable upload. Returns (video_id, privacy actually applied)."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    youtube = _service()
    body = {
        "snippet": {
            "title": _safe_title(title),
            "description": description,
            "categoryId": YOUTUBE_CATEGORY_ID,
        },
        "status": {"privacyStatus": _privacy(), "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=_CHUNK_BYTES, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    failures = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status not in _RETRY_STATUSES or failures >= _MAX_CHUNK_RETRIES:
                raise
            failures += 1
        except (OSError, ConnectionError) as e:
            if failures >= _MAX_CHUNK_RETRIES:
                raise
            failures += 1
            logger.warning("YouTube chunk upload hit %s; retry %d", e, failures)
        else:
            failures = 0
            continue
        time.sleep(min(2 ** failures, 60))

    video_id = response.get("id")
    if not video_id:
        raise YouTubeError(f"Upload finished without a video id: {response}")
    return video_id, response.get("status", {}).get("privacyStatus", _privacy())


def check_status(video_id: str) -> tuple[str, str]:
    """Blocking. Returns (state, detail); state is one of
    'processed', 'processing', 'failed', 'missing'."""
    resp = _service().videos().list(part="status", id=video_id).execute()
    items = resp.get("items") or []
    if not items:
        return "missing", "video not found on YouTube"
    status = items[0].get("status", {})
    upload_status = status.get("uploadStatus", "")
    if upload_status == "processed":
        return "processed", ""
    if upload_status == "uploaded":
        return "processing", ""
    detail = status.get("rejectionReason") or status.get("failureReason") or upload_status
    return "failed", detail or "unknown"


# ── marker ────────────────────────────────────────────────────────────────────

def marker_path(folder: Path) -> Path:
    return folder / MARKER_NAME


def read_marker(folder: Path) -> Optional[dict]:
    try:
        data = json.loads(marker_path(folder).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("video_id") else None
    except Exception:
        return None


def _write_marker(folder: Path, data: dict) -> None:
    marker_path(folder).write_text(json.dumps(data, indent=2), encoding="utf-8")


def expiry_of(marker: dict) -> datetime:
    uploaded = datetime.fromisoformat(marker["uploaded_at"])
    return uploaded + timedelta(days=YOUTUBE_RETENTION_DAYS)


# ── orchestration ─────────────────────────────────────────────────────────────

async def upload_recording(mp4: Path) -> dict:
    """Upload `mp4`, record the marker, and wait for YouTube to process it.

    Returns the marker dict (status 'processed' or 'processing'). Raises
    YouTubeError / the underlying exception on failure; in that case no marker
    is left behind, so the folder is never auto-deleted.
    """
    folder = mp4.parent
    marker_path(folder).unlink(missing_ok=True)

    # Only the byte transfer is serialized; waiting for YouTube to process
    # (below) must not hold up the next recording's upload.
    async with _upload_lock:
        video_id, privacy = await asyncio.to_thread(
            upload_file, mp4, folder.name, f"Recorded with Zoomy — {folder.name}"
        )
    marker = {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "privacy": privacy,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "status": "processing",
    }
    _write_marker(folder, marker)

    deadline = time.monotonic() + POLL_TIMEOUT_SECS
    while True:
        state, detail = await asyncio.to_thread(check_status, video_id)
        if state == "processed":
            marker["status"] = "processed"
            _write_marker(folder, marker)
            return marker
        if state in {"failed", "missing"}:
            marker_path(folder).unlink(missing_ok=True)
            raise YouTubeError(f"YouTube did not accept the video: {detail}")
        if time.monotonic() >= deadline:
            return marker
        await asyncio.sleep(POLL_INTERVAL_SECS)


def cleanup_expired(
    root: Path,
    is_active_folder: Callable[[Path], bool] = lambda _p: False,
    now: Optional[datetime] = None,
) -> list[Path]:
    """Blocking. Delete every recording folder whose marker has expired and
    whose video YouTube still reports as processed. Returns deleted folders."""
    now = now or datetime.now(timezone.utc)
    deleted: list[Path] = []
    if not root.is_dir():
        return deleted

    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        marker = read_marker(folder)
        if not marker:
            continue
        try:
            if now < expiry_of(marker):
                continue
        except (KeyError, ValueError):
            logger.warning("Unreadable marker in %s — leaving it alone", folder)
            continue
        if is_active_folder(folder):
            continue
        try:
            state, detail = check_status(marker["video_id"])
        except Exception as e:
            logger.warning("Cannot verify YouTube video for %s (%s) — keeping folder", folder.name, e)
            continue
        if state != "processed":
            logger.warning("YouTube video for %s is '%s' (%s) — keeping folder", folder.name, state, detail)
            continue
        try:
            shutil.rmtree(folder)
        except Exception as e:
            logger.warning("Could not delete %s: %s", folder, e)
            continue
        logger.info("Deleted expired recording folder %s (video %s)", folder.name, marker["video_id"])
        deleted.append(folder)
    return deleted
