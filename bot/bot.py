import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import logging
import logging.handlers
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import dateparser as dateparser_lib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import transcriber as transcriber_mod
import summarizer as summarizer_mod

from recorder import ZoomRecorder, _display_pool, SILENT_SNOOZE_SECS
from store import RecordingSession, SessionStore


def _setup_logging() -> None:
    log_dir = Path(os.environ.get("RECORDINGS_DIR", "/recordings")) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "zoomy.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
AUTHORIZED_IDS = {
    int(x.strip())
    for x in os.environ.get("AUTHORIZED_USER_IDS", "").split(",")
    if x.strip()
}
DEFAULT_GUEST_NAME = os.environ.get("GUEST_NAME", "zoomy.ircham.dev")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "id") or None

WIB = ZoneInfo("Asia/Jakarta")

DATEPARSER_SETTINGS = {
    "TIMEZONE": "Asia/Jakarta",
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "future",
    "PREFER_DAY_OF_MONTH": "first",
}

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
SCHEDULES_FILE = RECORDINGS_DIR / "schedules.json"


def _normalize_time_input(text: str) -> str:
    """Normalize time separators so dateparser handles them correctly.
    Converts period-separated times like '13.30' or '9.00' to '13:30' / '9:00'.
    """
    return re.sub(r'\b(\d{1,2})\.(\d{2})\b', r'\1:\2', text)


# ── Schedule file helpers ─────────────────────────────────────────────────────

def _read_schedule_file() -> list[dict]:
    try:
        if SCHEDULES_FILE.exists():
            return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read schedules file: %s", e)
    return []


def _write_schedule_file(entries: list[dict]) -> None:
    try:
        SCHEDULES_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not write schedules file: %s", e)


def _add_to_schedule_file(entry: dict) -> None:
    entries = _read_schedule_file()
    entries.append(entry)
    _write_schedule_file(entries)


def _remove_from_schedule_file(sched_id: int) -> None:
    entries = _read_schedule_file()
    entries = [e for e in entries if e.get("sched_id") != sched_id]
    _write_schedule_file(entries)

store = SessionStore()

_transcription_queue: asyncio.Queue = asyncio.Queue()
_transcription_busy: bool = False          # True while worker is actively transcribing
_pending_transcriptions: dict[str, str] = {}  # session_key → mp4_path
_pending_send_transcript: dict[str, str] = {}  # token → txt_path
_pending_summarize_txt: dict[str, str] = {}   # token → txt_path
_pending_summary_context: dict[int, dict] = {}
# {user_id: {txt_path, folder_name, chat_id, prompt_msg_id, timeout_task}}

_reschedule_state: dict[int, dict] = {}
# {user_id: {sched_id, chat_id, confirm_msg_id, pending_dt, timeout_task}}

_RENAME_FORBIDDEN = set('/\\:*?"<>|')  # chars not allowed in folder names


def _queue_status_str() -> str:
    """Call after put()-ing an item. Returns human-readable position."""
    qs = _transcription_queue.qsize()
    if not _transcription_busy and qs == 1:
        return "Starting now…"
    return f"Queued — #{qs} in line"


def _extract_file_prefix(folder_name: str) -> str:
    """Return the file stem used inside a recording folder.
    Strips a trailing _YYYYMMDD suffix if present, otherwise returns the full name.
    e.g. 'standup_20250512' → 'standup', 'my-meeting' → 'my-meeting'
    """
    m = re.match(r'^(.+)_\d{8}$', folder_name)
    return m.group(1) if m else folder_name


async def _resolve_url(url: str) -> str:
    """Follow HTTP redirects and return the final URL. Returns original on any error."""
    import urllib.request

    def _follow() -> str:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; ZoomyBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url

    try:
        resolved = await asyncio.to_thread(_follow)
        if resolved != url:
            logger.info("URL resolved: %s → %s", url, resolved)
        return resolved
    except Exception as e:
        logger.warning("URL resolution failed for %s: %s", url, e)
        return url


async def _preprocess_input(text: str) -> str:
    """If input looks like a non-Zoom/Meet URL, follow redirects to find the real URL."""
    text = text.strip()
    try:
        p = urlparse(text)
        netloc = p.netloc.lower()
        if (p.scheme in ("http", "https") and netloc
                and "zoom.us" not in netloc
                and "meet.google.com" not in netloc):
            return await _resolve_url(text)
    except Exception:
        pass
    return text


def _parse_meeting_input(text: str) -> tuple[str | None, str | None, bool, str | None]:
    """Parse user input into a normalized meeting URL for Zoom or Google Meet.

    Returns (normalized_url, platform, has_password, error_message).
    platform is 'zoom' or 'google_meet'. normalized_url is None on error.

    Zoom accepts:
      - Full URL with pwd: https://zoom.us/j/97856007427?pwd=xxx → has_password=True
      - Full URL, no pwd:  https://zoom.us/j/97856007427         → has_password=False
      - Meeting ID only:   97856007427 or 978 5600 7427          → has_password=False

    Google Meet accepts:
      - https://meet.google.com/xxx-xxxx-xxx
    """
    text = text.strip()

    # Google Meet URL
    try:
        p = urlparse(text)
        if "meet.google.com" in p.netloc.lower():
            path = p.path.strip("/")
            if re.match(r'^[a-z]+-[a-z]+-[a-z]+$', path):
                return text, "google_meet", False, None
            return None, None, False, (
                "Format Google Meet tidak valid.\n"
                "Contoh: <code>https://meet.google.com/xxx-xxxx-xxx</code>"
            )
    except Exception:
        pass

    # Zoom — Meeting ID only (9-11 digits, spaces/dashes allowed)
    meeting_id = re.sub(r'[\s\-]', '', text)
    if re.fullmatch(r'\d{9,11}', meeting_id):
        return f"https://zoom.us/j/{meeting_id}", "zoom", False, None

    # Zoom URL
    try:
        p = urlparse(text)
    except Exception:
        return None, None, False, (
            "Format tidak dikenali.\n"
            "Kirim URL Zoom, Meeting ID (9–11 digit), atau URL Google Meet."
        )

    host = p.netloc.lower()
    if host == "zoom.us" or host.endswith(".zoom.us"):
        if not re.search(r'/(?:j|wc)/(\d{9,11})(?:/|$|\?)', p.path):
            return None, None, False, (
                "Meeting ID tidak ditemukan dalam URL.\n"
                "Format yang valid: zoom.us/j/1234567890"
            )
        has_pwd = "pwd" in parse_qs(p.query)
        return text, "zoom", has_pwd, None

    return None, None, False, (
        "Bukan link Zoom atau Google Meet yang valid.\n"
        "Kirim URL zoom.us, meeting ID, atau meet.google.com."
    )



# ── Scheduled recordings ──────────────────────────────────────────────────────

@dataclass
class ScheduledRecording:
    sched_id: int
    user_id: int
    chat_id: int
    scheduled_time: datetime        # WIB-aware
    description: str
    task: asyncio.Task

_scheduled: dict[int, list[ScheduledRecording]] = {}  # user_id → list
_sched_counter: int = 0


async def _transcription_worker(bot) -> None:
    global _transcription_busy
    while True:
        mp4_path, chat_id, session_key, model = await _transcription_queue.get()
        _transcription_busy = True
        is_retranscribe = session_key.startswith("retranscribe/")
        label = session_key[len("retranscribe/"):] if is_retranscribe else f"Session {session_key}"
        verb = "Retranscribing" if is_retranscribe else "Transcribing"

        await bot.send_message(chat_id, f"{verb} {label} ({model})…")
        try:
            txt, srt = await transcriber_mod.transcribe(mp4_path, model, WHISPER_LANGUAGE)
            txt_obj = Path(txt)

            # Update .metadata.json with transcript info
            meta_path = txt_obj.parent / ".metadata.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta["transcript"] = {
                        "model": model,
                        "transcribed_at": datetime.now().isoformat(),
                    }
                    meta_path.write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception:
                    logger.warning("Could not update metadata after transcription")

            token_send = secrets.token_hex(8)
            _pending_send_transcript[token_send] = str(txt_obj)
            done_verb = "Retranscription done" if is_retranscribe else "Transcript saved"
            kb_rows = [[InlineKeyboardButton("📄 Send Transcript", callback_data=f"send_txt:{token_send}")]]
            if summarizer_mod.is_configured():
                token_sum = secrets.token_hex(8)
                _pending_summarize_txt[token_sum] = str(txt_obj)
                kb_rows.append([InlineKeyboardButton("🤖 AI Summary", callback_data=f"summarize_txt:{token_sum}")])
            await bot.send_message(
                chat_id,
                f"✅ {done_verb} — {label} ({model})\n  {txt_obj.stem}.txt\n  {txt_obj.stem}.srt",
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )
        except Exception as e:
            await bot.send_message(chat_id, f"Transcription failed ({label}): {e}")
        finally:
            _transcription_busy = False
            _transcription_queue.task_done()


async def _transcribe_timeout(session_key: str, mp4_path: str, chat_id: int, bot) -> None:
    await asyncio.sleep(100)
    if session_key in _pending_transcriptions:
        _pending_transcriptions.pop(session_key)
        await _transcription_queue.put((mp4_path, chat_id, session_key, WHISPER_MODEL))
        await bot.send_message(
            chat_id,
            f"Auto-transcribing session {session_key} ({WHISPER_MODEL}). {_queue_status_str()}"
        )


async def _handle_transcribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, session_key = query.data.split(":", 1)

    _MODEL_MAP = {
        "transcribe_medium": "medium",
        "transcribe_large":  "large-v3",
    }
    if action in _MODEL_MAP:
        mp4_path = _pending_transcriptions.pop(session_key, None)
        if not mp4_path:
            await query.edit_message_text(query.message.text + "\n\n⚠️ Request expired.")
            return
        model = _MODEL_MAP[action]
        await _transcription_queue.put((mp4_path, query.message.chat_id, session_key, model))
        await query.edit_message_text(
            query.message.text + f"\n\nTranscription ({model}): {_queue_status_str()}"
        )

    elif action == "skip":
        _pending_transcriptions.pop(session_key, None)
        await query.edit_message_text(query.message.text + "\n\nTranscription skipped.")


async def cb_send_txt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    txt_path = _pending_send_transcript.pop(token, None)
    if not txt_path or not Path(txt_path).exists():
        await query.edit_message_text(query.message.text + "\n\n⚠️ File not found or already sent.")
        return
    try:
        with open(txt_path, "rb") as f:
            await context.bot.send_document(
                query.message.chat_id, document=f, filename=Path(txt_path).name
            )
        if summarizer_mod.is_configured():
            token_sum = secrets.token_hex(8)
            _pending_summarize_txt[token_sum] = txt_path
            await query.edit_message_text(
                query.message.text + "\n\n📄 Sent.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🤖 AI Summary", callback_data=f"summarize_txt:{token_sum}"),
                ]]),
            )
        else:
            await query.edit_message_text(query.message.text + "\n\n📄 Sent.")
    except Exception as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ Failed to send: {e}")


def is_authorized(user_id: int) -> bool:
    return not AUTHORIZED_IDS or user_id in AUTHORIZED_IDS


def _session_label(s: RecordingSession) -> str:
    name = s.recorder.recording_prefix or s.url[:30]
    return f"Session {s.session_num}: {name}"


# ── /record ───────────────────────────────────────────────────────────────────

async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("/record — user=%s(%d)", user.username or user.first_name, user.id)
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = user.id

    if store.has_pending(user_id):
        await update.message.reply_text("Setup already in progress. Complete it first.")
        return

    url = context.args[0] if context.args else None
    active = store.active(user_id)

    raw = context.args[0] if context.args else None

    if raw:
        raw = await _preprocess_input(raw)
        normalized_url, platform, has_pwd, err = _parse_meeting_input(raw)
        if err:
            await update.message.reply_text(f"⚠️ {err}", parse_mode="HTML")
            return
        url = normalized_url
        if any(s.url == url for s in active):
            await update.message.reply_text("Already recording this meeting.")
            return
    else:
        url = None
        platform = None

    store.set_pending(user_id, {
        "url": url,
        "platform": platform,
        "guest_name": DEFAULT_GUEST_NAME,
        "resolution": "1080p",
        "state": None,
        "timeout_task": None,
    })

    # No input provided — ask for it
    if not url:
        store.update_pending(user_id, state="input_url")
        await update.message.reply_text(
            "Kirim Zoom URL atau Meeting ID: (100s timeout)\n\n"
            "Contoh:\n"
            "• <code>https://zoom.us/j/97856007427?pwd=xxx</code>\n"
            "• <code>https://zoom.us/j/97856007427</code>\n"
            "• <code>97856007427</code>",
            parse_mode="HTML",
        )
        task = asyncio.create_task(_url_timeout(user_id, context.bot))
        store.update_pending(user_id, timeout_task=task)
        return

    if active:
        lines = "\n".join(f"• {_session_label(s)}" for s in active)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, Start", callback_data="confirm_new_session"),
            InlineKeyboardButton("Cancel", callback_data="cancel_new_session"),
        ]])
        await update.message.reply_text(
            f"Currently recording:\n{lines}\n\nStart another recording?",
            reply_markup=kb,
        )
        return

    if not has_pwd and platform == "zoom":
        await _ask_meeting_password(user_id, context.bot)
        return

    await _send_name_keyboard(user_id, context.bot)


async def _url_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_url":
        store.pop_pending(user_id)
        await bot.send_message(user_id, "Timed out — no URL received. Send /record to try again.")


async def _ask_meeting_password(user_id: int, bot) -> None:
    _cancel_timeout(user_id)
    store.update_pending(user_id, state="input_pwd")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_pwd")]])
    await bot.send_message(
        user_id,
        "🔑 URL tidak menyertakan password. Kirim password meeting-nya, "
        "atau tekan <b>Skip</b> jika tidak ada. (100s timeout)",
        parse_mode="HTML",
        reply_markup=kb,
    )
    task = asyncio.create_task(_pwd_timeout(user_id, bot))
    store.update_pending(user_id, timeout_task=task)


async def _pwd_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_pwd":
        store.update_pending(user_id, state=None)
        await bot.send_message(user_id, "Timed out — proceeding without password.")
        await _send_name_keyboard(user_id, bot)


async def cb_skip_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if store.get_pending(user_id).get("state") != "input_pwd":
        return
    _cancel_timeout(user_id)
    store.update_pending(user_id, state=None)
    await query.edit_message_text("No password — proceeding.")
    await _send_name_keyboard(user_id, context.bot)


async def _send_name_keyboard(user_id: int, bot) -> None:
    _cancel_timeout(user_id)
    store.update_pending(user_id, state="waiting_name")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Change Bot Name", callback_data="change_name"),
        InlineKeyboardButton(f"Use {DEFAULT_GUEST_NAME}", callback_data="use_default"),
    ]])
    await bot.send_message(
        user_id,
        f"Bot will join as '{DEFAULT_GUEST_NAME}'. Change for this session? (auto in 100s)",
        reply_markup=kb,
    )
    task = asyncio.create_task(_name_timeout(user_id, bot))
    store.update_pending(user_id, timeout_task=task)


async def _name_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "waiting_name":
        store.update_pending(user_id, state=None)
        await bot.send_message(user_id, f"No response — using {DEFAULT_GUEST_NAME}.")
        await _ask_recording_name(user_id, bot)


# ── Concurrent session confirmation ───────────────────────────────────────────

async def cb_confirm_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return

    await query.edit_message_text("Starting new session...")
    await _send_name_keyboard(user_id, context.bot)


async def cb_cancel_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    store.pop_pending(update.effective_user.id)
    await query.edit_message_text("Cancelled.")


# ── Name step ─────────────────────────────────────────────────────────────────

async def cb_use_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return

    _cancel_timeout(user_id)
    store.update_pending(user_id, state=None)
    await query.edit_message_text(f"Using name: {DEFAULT_GUEST_NAME}")
    await _ask_recording_name(user_id, context.bot)


async def cb_change_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired.")
        return

    _cancel_timeout(user_id)
    store.update_pending(user_id, state="input_name")
    await query.edit_message_text("Type the bot name for this session:")


# ── Recording name step ───────────────────────────────────────────────────────

async def _ask_recording_name(user_id: int, bot) -> None:
    _cancel_timeout(user_id)
    store.update_pending(user_id, state="input_rec_name")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_rec_name")]])
    await bot.send_message(
        user_id,
        "Recording name? (auto-name in 100s)",
        reply_markup=kb,
    )
    task = asyncio.create_task(_rec_name_timeout(user_id, bot))
    store.update_pending(user_id, timeout_task=task)


async def _rec_name_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_rec_name":
        store.update_pending(user_id, state=None)
        await bot.send_message(user_id, "No response — using auto-name.")
        await _ask_start_or_schedule(user_id, bot)


async def cb_skip_rec_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if store.get_pending(user_id).get("state") != "input_rec_name":
        return

    _cancel_timeout(user_id)
    store.update_pending(user_id, state=None)
    await query.edit_message_text("Using auto-name.")
    await _ask_start_or_schedule(user_id, context.bot)


# ── Message router ────────────────────────────────────────────────────────────

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id

    # Reschedule input takes priority over everything
    if user_id in _reschedule_state:
        await _handle_reschedule_input(update, context)
        return

    # Rename input (sub-state inside history session)
    hist = _history_state.get(user_id)
    if hist and hist.get("sub_state") == "input_rename":
        await _handle_rename_input(update, context)
        return

    # AI Summary context input
    if user_id in _pending_summary_context:
        await _handle_summary_context_input(update, context)
        return

    state = store.get_pending(user_id).get("state")

    if state == "input_url":
        raw_input = await _preprocess_input(update.message.text)
        normalized_url, platform, has_pwd, err = _parse_meeting_input(raw_input)
        if err:
            await update.message.reply_text(f"⚠️ {err}\n\nCoba kirim lagi:", parse_mode="HTML")
            return  # keep state + timeout running so user can retry
        _cancel_timeout(user_id)
        active = store.active(user_id)
        if any(s.url == normalized_url for s in active):
            store.pop_pending(user_id)
            await update.message.reply_text("Already recording this meeting.")
            return
        store.update_pending(user_id, url=normalized_url, platform=platform, state=None)
        await update.message.reply_text("✅ Diterima.")
        if active:
            lines = "\n".join(f"• {_session_label(s)}" for s in active)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, Start", callback_data="confirm_new_session"),
                InlineKeyboardButton("Cancel", callback_data="cancel_new_session"),
            ]])
            await context.bot.send_message(
                user_id,
                f"Currently recording:\n{lines}\n\nStart another recording?",
                reply_markup=kb,
            )
            return
        if not has_pwd and platform == "zoom":
            await _ask_meeting_password(user_id, context.bot)
            return
        await _send_name_keyboard(user_id, context.bot)

    elif state == "input_pwd":
        pwd = update.message.text.strip()
        _cancel_timeout(user_id)
        store.update_pending(user_id, meeting_password=pwd, state=None)
        await update.message.reply_text("🔑 Password diterima.")
        await _send_name_keyboard(user_id, context.bot)

    elif state == "input_name":
        name = update.message.text.strip()
        store.update_pending(user_id, guest_name=name, state=None)
        await update.message.reply_text(f"Bot name set: {name}")
        await _ask_recording_name(user_id, context.bot)

    elif state == "input_rec_name":
        prefix = update.message.text.strip()
        _cancel_timeout(user_id)
        store.update_pending(user_id, state=None, prefix=prefix)
        await update.message.reply_text(f"Recording name: {prefix}")
        await _ask_start_or_schedule(user_id, context.bot)

    elif state == "input_schedule_time":
        text = _normalize_time_input(update.message.text.strip())
        dt = dateparser_lib.parse(text, settings=DATEPARSER_SETTINGS)
        if not dt:
            await update.message.reply_text(
                "Couldn't understand that time. Try something like:\n"
                "`tomorrow 14:00` · `in 2 hours` · `May 15 09:30`",
                parse_mode="Markdown",
            )
            return  # keep state, keep timeout running

        now_utc = datetime.now(timezone.utc)
        delay = (dt - now_utc).total_seconds()
        if delay <= 0:
            await update.message.reply_text(
                "That time is already in the past. Try again."
            )
            return  # keep state, keep timeout running

        _cancel_timeout(user_id)
        p = store.pop_pending(user_id)
        dt_wib = dt.astimezone(WIB)
        time_str = dt_wib.strftime("%A, %d %b %Y at %H:%M WIB")

        global _sched_counter
        _sched_counter += 1
        sched_id = _sched_counter

        description = p.get("prefix") or "auto-name"
        task = asyncio.create_task(
            _run_scheduled(sched_id, user_id, update.effective_chat.id, context.bot, p, delay, dt_wib)
        )
        sched = ScheduledRecording(
            sched_id=sched_id,
            user_id=user_id,
            chat_id=update.effective_chat.id,
            scheduled_time=dt_wib,
            description=description,
            task=task,
        )
        _scheduled.setdefault(user_id, []).append(sched)

        # Persist to file — strip non-serialisable keys from pending data
        _add_to_schedule_file({
            "sched_id": sched_id,
            "user_id": user_id,
            "chat_id": update.effective_chat.id,
            "scheduled_time": dt.isoformat(),   # UTC-aware ISO string
            "description": description,
            "pending": {k: v for k, v in p.items() if k not in ("state", "timeout_task")},
        })

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_schedule_{sched_id}"),
        ]])
        await update.message.reply_text(
            f"✅ Scheduled!\n\n"
            f"🕐 {time_str}\n"
            f"📝 Recording: {html.escape(description)}\n"
            f"👤 Bot name: {html.escape(p.get('guest_name', DEFAULT_GUEST_NAME))}",
            reply_markup=kb,
        )



async def _ask_start_or_schedule(user_id: int, bot) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Start Now", callback_data="start_now"),
        InlineKeyboardButton("🕐 Schedule",  callback_data="schedule_later"),
    ]])
    await bot.send_message(user_id, "Start now or schedule for later?", reply_markup=kb)


async def cb_start_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return
    await query.edit_message_text("Starting…")
    await _start_recording(user_id, update.effective_chat.id, context.bot)


async def cb_schedule_later(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return
    store.update_pending(user_id, state="input_schedule_time")
    await query.edit_message_text(
        "When should I join? (all times in WIB / UTC+7)\n\n"
        "Examples:\n"
        "• `tomorrow 14:00`\n"
        "• `in 2 hours`\n"
        "• `May 15 09:30`\n"
        "• `Friday 08:00`",
        parse_mode="Markdown",
    )
    task = asyncio.create_task(_schedule_input_timeout(user_id, context.bot))
    store.update_pending(user_id, timeout_task=task)


async def _schedule_input_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_schedule_time":
        store.pop_pending(user_id)
        await bot.send_message(user_id, "Timed out — scheduling cancelled.")


def _cancel_timeout(user_id: int) -> None:
    task = store.get_pending(user_id).get("timeout_task")
    if task and not task.done():
        task.cancel()


# ── Start recording ───────────────────────────────────────────────────────────

async def _start_recording(user_id: int, chat_id: int, bot) -> None:
    """Pop pending state and launch recording."""
    p = store.pop_pending(user_id)
    await _launch_recording(user_id, chat_id, bot, p)


async def _launch_recording(user_id: int, chat_id: int, bot, p: dict) -> None:
    """Core recording launch — accepts already-extracted pending data dict."""
    url = p.get("url")
    guest_name = p.get("guest_name", DEFAULT_GUEST_NAME)
    prefix = p.get("prefix")
    resolution = p.get("resolution", "1080p")
    meeting_password = p.get("meeting_password")

    if not url:
        await bot.send_message(chat_id, "Error: no URL found.")
        return

    try:
        display_num, display_str, sink_monitor = await _display_pool.acquire()
    except Exception as e:
        logger.exception("Failed to acquire display for user %d", user_id)
        await bot.send_message(chat_id, f"Error starting session: {e}")
        return

    store.prune(user_id)
    session_num = store.next_num(user_id)
    logger.info(
        "Session %d starting — user_id=%d url=%s guest=%s prefix=%s res=%s display=%s",
        session_num, user_id, url, guest_name, prefix or "auto", resolution, display_str,
    )

    async def on_started(filename: str) -> None:
        folder = Path(filename).parent.name
        await bot.send_message(
            chat_id,
            f"Recording started — Session {session_num}\nFolder: {folder}",
        )
        session = store.find(user_id, session_num)
        if session:
            await _send_peek(session, chat_id, bot)

    async def on_stopped(filename: str, duration: str, size: str, auto_ended: bool = False) -> None:
        folder = Path(filename).parent.name
        reason = "Meeting ended by host" if auto_ended else "Stopped manually"
        session_key = str(session_num)
        _pending_transcriptions[session_key] = filename
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎙 Medium", callback_data=f"transcribe_medium:{session_key}"),
                InlineKeyboardButton("🎙 Large",  callback_data=f"transcribe_large:{session_key}"),
            ],
            [InlineKeyboardButton("Skip", callback_data=f"skip:{session_key}")],
        ])
        await bot.send_message(
            chat_id,
            f"Recording saved — Session {session_num}\n"
            f"Folder: {folder}\nDuration: {duration}\nSize: {size}\nReason: {reason}\n\n"
            f"Recording can be viewed at file.ircham.dev\n\n"
            f"Transcribe with Whisper? (auto-transcribes with Medium in 100s)",
            reply_markup=keyboard,
        )
        asyncio.create_task(_transcribe_timeout(session_key, filename, chat_id, bot))

    async def on_error(msg: str) -> None:
        await bot.send_message(chat_id, f"Session {session_num} error: {msg}")

    async def on_dialog(dialog_text: str) -> None:
        short = dialog_text[:300].strip()
        if len(short) < 10 or short.lower() == "notification":
            return
        await bot.send_message(
            chat_id,
            f"ℹ️ <b>Session {session_num} — dialog dismissed:</b>\n<i>{html.escape(short)}</i>",
            parse_mode="HTML",
        )

    async def on_waiting(msg: str) -> None:
        await bot.send_message(chat_id, f"⏸ Session {session_num} — {msg}")

    async def on_silence_warn(duration: int) -> None:
        name = html.escape(recorder.recording_prefix or "recording")
        snooze_mins = SILENT_SNOOZE_SECS // 60
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💤 Remind me in {snooze_mins}m",    callback_data=f"silence_snooze:{session_num}")],
            [InlineKeyboardButton("🔔 Alert when audio returns",         callback_data=f"silence_wait:{session_num}")],
            [InlineKeyboardButton("⏹ Stop recording",                   callback_data=f"stop_session_{session_num}")],
        ])
        await bot.send_message(
            chat_id,
            f"🔇 <b>Session {session_num} — {name}</b>\n"
            f"Zoom meeting room is silent for <b>{duration}s</b> — still recording.",
            parse_mode="HTML",
            reply_markup=kb,
        )

    async def on_audio_returned() -> None:
        name = html.escape(recorder.recording_prefix or "recording")
        await bot.send_message(
            chat_id,
            f"🔊 <b>Session {session_num} — {name}</b>\n"
            f"Audio detected — recording is active.",
            parse_mode="HTML",
        )

    recorder = ZoomRecorder(
        display=display_str,
        sink=sink_monitor,
        guest_name=guest_name,
        recording_prefix=prefix,
        resolution=resolution,
        meeting_password=meeting_password,
        on_started=on_started,
        on_stopped=on_stopped,
        on_error=on_error,
        on_dialog=on_dialog,
        on_waiting=on_waiting,
        on_silence_warn=on_silence_warn,
        on_audio_returned=on_audio_returned,
    )

    async def _hourly_warning() -> None:
        hour = 0
        while recorder.is_recording:
            await asyncio.sleep(3600)
            hour += 1
            if not recorder.is_recording:
                break
            name = html.escape(recorder.recording_prefix or "recording")
            await bot.send_message(
                chat_id,
                f"⏱ <b>Session {session_num} — {name}</b> still running\n"
                f"Duration: {hour}h | Size: {recorder.file_size_str()}",
                parse_mode="HTML",
            )

    async def run() -> None:
        warning_task = asyncio.create_task(_hourly_warning())
        try:
            await recorder.record(url)
        finally:
            warning_task.cancel()
            try:
                await _display_pool.release(display_num)
            except Exception:
                logger.exception("Failed to release display %d for session %d", display_num, session_num)

    def _on_task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and (exc := task.exception()):
            logger.error("Unhandled exception in session %d task", session_num, exc_info=exc)

    task = asyncio.create_task(run())
    task.add_done_callback(_on_task_done)
    store.add(user_id, RecordingSession(session_num, url, recorder, task, display_num))


# ── Scheduled recording runner ────────────────────────────────────────────────

async def _run_scheduled(
    sched_id: int, user_id: int, chat_id: int, bot, p: dict, delay: float, dt_wib: datetime
) -> None:
    try:
        await asyncio.sleep(delay)
        _scheduled[user_id] = [
            s for s in _scheduled.get(user_id, []) if s.sched_id != sched_id
        ]
        _remove_from_schedule_file(sched_id)
        time_str = dt_wib.strftime("%H:%M WIB")
        await bot.send_message(chat_id, f"🕐 Starting scheduled recording ({time_str})…")
        await _launch_recording(user_id, chat_id, bot, p)
    except asyncio.CancelledError:
        pass


# ── /unschedule ───────────────────────────────────────────────────────────────

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = user.id
    pending = [s for s in _scheduled.get(user_id, []) if not s.task.done()]
    if not pending:
        await update.message.reply_text("No scheduled recordings.")
        return

    lines = []
    buttons = []
    for s in pending:
        time_str = s.scheduled_time.strftime("%a %d %b at %H:%M WIB")
        lines.append(f"• #{s.sched_id} — {html.escape(s.description)} @ {time_str}")
        buttons.append([
            InlineKeyboardButton("🔄 Reschedule", callback_data=f"reschedule_{s.sched_id}"),
            InlineKeyboardButton("❌ Cancel",      callback_data=f"cancel_schedule_{s.sched_id}"),
        ])
    await update.message.reply_text(
        "Scheduled recordings:\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_cancel_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    sched_id = int(query.data.split("_")[-1])

    user_scheds = _scheduled.get(user_id, [])
    sched = next((s for s in user_scheds if s.sched_id == sched_id), None)
    if not sched or sched.task.done():
        await query.edit_message_text(query.message.text + "\n\n⚠️ Already completed or not found.")
        return

    sched.task.cancel()
    _scheduled[user_id] = [s for s in user_scheds if s.sched_id != sched_id]
    _remove_from_schedule_file(sched_id)
    time_str = sched.scheduled_time.strftime("%a %d %b at %H:%M WIB")
    await query.edit_message_text(f"❌ Cancelled: #{sched_id} — {html.escape(sched.description)} @ {time_str}")


async def cb_reschedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    sched_id = int(re.search(r"\d+", query.data).group())

    user_scheds = _scheduled.get(user_id, [])
    sched = next((s for s in user_scheds if s.sched_id == sched_id), None)
    if not sched or sched.task.done():
        await query.edit_message_text(query.message.text + "\n\n⚠️ Already fired or cancelled.")
        return

    _reschedule_state[user_id] = {
        "sched_id": sched_id,
        "chat_id": query.message.chat_id,
        "confirm_msg_id": query.message.message_id,
        "pending_dt": None,
        "timeout_task": None,
    }
    time_str = sched.scheduled_time.strftime("%a %d %b at %H:%M WIB")
    await query.edit_message_text(
        f"🔄 Rescheduling: <b>{html.escape(sched.description)}</b>\n"
        f"Current time: {time_str}\n\n"
        f"Send the new date/time: (100s timeout)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Abort", callback_data="reschedule_abort"),
        ]]),
    )
    _reset_reschedule_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def _handle_reschedule_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = _reschedule_state.get(user_id)
    if not state:
        return

    text = _normalize_time_input(update.message.text.strip())
    dt = dateparser_lib.parse(text, settings=DATEPARSER_SETTINGS)
    if not dt:
        await update.message.reply_text(
            "Couldn't understand that time. Try:\n"
            "`tomorrow 14:00` · `in 2 hours` · `May 15 09:30`",
            parse_mode="Markdown",
        )
        return  # keep state + timeout running

    if (dt - datetime.now(timezone.utc)).total_seconds() <= 0:
        await update.message.reply_text("That time is already in the past. Try again.")
        return  # keep state + timeout running

    dt_wib = dt.astimezone(WIB)
    time_str = dt_wib.strftime("%A, %d %b %Y at %H:%M WIB")
    state["pending_dt"] = dt

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="reschedule_confirm"),
        InlineKeyboardButton("✏️ Change",  callback_data="reschedule_change"),
    ], [
        InlineKeyboardButton("❌ Abort", callback_data="reschedule_abort"),
    ]])
    try:
        await context.bot.edit_message_text(
            chat_id=state["chat_id"],
            message_id=state["confirm_msg_id"],
            text=f"🕐 New time: <b>{html.escape(time_str)}</b>\n\nConfirm?",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        msg = await update.message.reply_text(
            f"🕐 New time: <b>{html.escape(time_str)}</b>\n\nConfirm?",
            reply_markup=kb,
            parse_mode="HTML",
        )
        state["confirm_msg_id"] = msg.message_id
        state["chat_id"] = update.effective_chat.id
    _reset_reschedule_timeout(user_id, context.bot, state["chat_id"], state["confirm_msg_id"])


async def cb_reschedule_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_reschedule_timeout(user_id)
    state = _reschedule_state.pop(user_id, None)
    if not state or not state.get("pending_dt"):
        await query.edit_message_text("Session expired.")
        return

    sched_id = state["sched_id"]
    dt = state["pending_dt"]

    entries = _read_schedule_file()
    entry = next((e for e in entries if e["sched_id"] == sched_id), None)
    user_scheds = _scheduled.get(user_id, [])
    sched = next((s for s in user_scheds if s.sched_id == sched_id), None)

    if not sched or sched.task.done() or not entry:
        await query.edit_message_text("⚠️ That recording already fired or was cancelled.")
        return

    now_utc = datetime.now(timezone.utc)
    delay = (dt - now_utc).total_seconds()
    if delay <= 0:
        await query.edit_message_text("⚠️ That time has already passed. Use /schedule to try again.")
        return

    dt_wib = dt.astimezone(WIB)
    time_str = dt_wib.strftime("%A, %d %b %Y at %H:%M WIB")
    p = entry["pending"]

    # Cancel old task and remove from store + file
    sched.task.cancel()
    _scheduled[user_id] = [s for s in user_scheds if s.sched_id != sched_id]
    _remove_from_schedule_file(sched_id)

    # Create new task, reuse same sched_id
    task = asyncio.create_task(
        _run_scheduled(sched_id, user_id, sched.chat_id, context.bot, p, delay, dt_wib)
    )
    _scheduled.setdefault(user_id, []).append(ScheduledRecording(
        sched_id=sched_id, user_id=user_id, chat_id=sched.chat_id,
        scheduled_time=dt_wib, description=sched.description, task=task,
    ))
    _add_to_schedule_file({
        "sched_id": sched_id, "user_id": user_id, "chat_id": sched.chat_id,
        "scheduled_time": dt.isoformat(), "description": sched.description,
        "pending": p,
    })

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_schedule_{sched_id}"),
    ]])
    await query.edit_message_text(
        f"✅ Rescheduled!\n\n"
        f"🕐 {time_str}\n"
        f"📝 Recording: {html.escape(sched.description)}",
        reply_markup=kb,
    )


async def cb_reschedule_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _reschedule_state.get(user_id)
    if not state:
        await query.edit_message_text("Session expired.")
        return
    sched_id = state["sched_id"]
    user_scheds = _scheduled.get(user_id, [])
    sched = next((s for s in user_scheds if s.sched_id == sched_id), None)
    desc = html.escape(sched.description) if sched else f"#{sched_id}"
    state["pending_dt"] = None
    await query.edit_message_text(
        f"🔄 Rescheduling: <b>{desc}</b>\n\nSend the new date/time: (100s timeout)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Abort", callback_data="reschedule_abort"),
        ]]),
    )
    _reset_reschedule_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def cb_reschedule_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_reschedule_timeout(user_id)
    _reschedule_state.pop(user_id, None)
    await query.edit_message_text("Rescheduling aborted.")


# ── /peek ─────────────────────────────────────────────────────────────────────

async def _send_peek(session: RecordingSession, chat_id: int, bot) -> None:
    """Grab one frame from the session's Xvfb display and send it to Telegram."""
    display = session.recorder.display
    tmp_path = Path(f"/tmp/peek_{chat_id}_{session.session_num}.png")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "x11grab", "-i", display,
                "-vframes", "1",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0 or not tmp_path.exists():
            stderr = result.stderr.decode(errors="replace")[-200:]
            logger.warning("peek ffmpeg failed (session %d): %s", session.session_num, stderr)
            await bot.send_message(chat_id, f"Session {session.session_num}: screenshot failed.")
            return
        name = html.escape(session.recorder.recording_prefix or "session")
        url = html.escape(session.recorder.current_url or "—")
        caption = (
            f"📸 Session {session.session_num} — {name}\n"
            f"⏱ {session.recorder.elapsed_str()} | 💾 {session.recorder.file_size_str()}\n"
            f"🔗 {url}"
        )
        with open(tmp_path, "rb") as f:
            await bot.send_photo(chat_id, photo=f, caption=caption)
    except subprocess.TimeoutExpired:
        await bot.send_message(chat_id, f"Session {session.session_num}: screenshot timed out.")
    except Exception as e:
        logger.exception("peek failed for session %d", session.session_num)
        await bot.send_message(chat_id, f"Session {session.session_num}: screenshot error: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


async def cmd_peek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("/peek — user=%s(%d)", user.username or user.first_name, user.id)
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = user.id
    active = store.active(user_id)

    if not active:
        await update.message.reply_text("No active recordings.")
        return

    if len(active) == 1:
        await update.message.reply_text("Taking screenshot…")
        await _send_peek(active[0], update.effective_chat.id, context.bot)
        return

    buttons = [
        [InlineKeyboardButton(_session_label(s), callback_data=f"peek_session_{s.session_num}")]
        for s in active
    ]
    buttons.append([InlineKeyboardButton("📸 All", callback_data="peek_all_sessions")])
    await update.message.reply_text(
        "Which session to peek?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_peek_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    session_num = int(query.data.split("_")[-1])
    session = store.find(user_id, session_num)
    if not session:
        await query.edit_message_text("Session not found or already stopped.")
        return
    await query.edit_message_text(f"Taking screenshot of session {session_num}…")
    await _send_peek(session, query.message.chat_id, context.bot)


async def cb_peek_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    active = store.active(user_id)
    if not active:
        await query.edit_message_text("No active sessions.")
        return
    await query.edit_message_text(f"Taking {len(active)} screenshot(s)…")
    for s in active:
        await _send_peek(s, query.message.chat_id, context.bot)


# ── /stop ─────────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("/stop — user=%s(%d)", user.username or user.first_name, user.id)
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = user.id
    active = store.active(user_id)

    if not active:
        await update.message.reply_text("No active recordings.")
        return

    if len(active) == 1:
        s = active[0]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, Stop", callback_data=f"stop_session_{s.session_num}"),
            InlineKeyboardButton("Cancel", callback_data="cancel_stop"),
        ]])
        await update.message.reply_text(f"Stop {_session_label(s)}?", reply_markup=kb)
        return

    buttons = [
        [InlineKeyboardButton(_session_label(s), callback_data=f"stop_session_{s.session_num}")]
        for s in active
    ]
    buttons.append([
        InlineKeyboardButton("Stop All", callback_data="stop_all_sessions"),
        InlineKeyboardButton("Cancel", callback_data="cancel_stop"),
    ])
    await update.message.reply_text(
        "Which session to stop?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_cancel_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")


async def cb_stop_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    session_num = int(query.data.split("_")[-1])
    session = store.find(user_id, session_num)
    if not session:
        await query.edit_message_text("Session not found or already stopped.")
        return

    await session.recorder.stop()
    await query.edit_message_text(f"Stopping {_session_label(session)}...")


async def cb_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    active = store.active(user_id)
    for s in active:
        await s.recorder.stop()
    await query.edit_message_text(f"Stopping all {len(active)} session(s)...")


# ── Silence-warning callbacks ─────────────────────────────────────────────────

async def cb_silence_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    session_num = int(query.data.split(":")[1])
    session = store.find(user_id, session_num)
    if not session:
        await query.edit_message_text(query.message.text + "\n\n⚠️ Session not found.")
        return
    session.recorder.silence_snooze()
    snooze_mins = SILENT_SNOOZE_SECS // 60
    name = html.escape(session.recorder.recording_prefix or "recording")
    await query.edit_message_text(
        f"💤 <b>Session {session_num} — {name}</b>\n"
        f"Snoozed for {snooze_mins}min — still recording.",
        parse_mode="HTML",
    )


async def cb_silence_wait(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    session_num = int(query.data.split(":")[1])
    session = store.find(user_id, session_num)
    if not session:
        await query.edit_message_text(query.message.text + "\n\n⚠️ Session not found.")
        return
    session.recorder.silence_wait_for_audio()
    name = html.escape(session.recorder.recording_prefix or "recording")
    await query.edit_message_text(
        f"⏸ <b>Session {session_num} — {name}</b>\n"
        f"Waiting for audio to return… Recording continues.",
        parse_mode="HTML",
    )


# ── /ongoing ──────────────────────────────────────────────────────────────────

async def cmd_ongoing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = update.effective_user.id
    active = store.active(user_id)
    if not active:
        await update.message.reply_text("No active recordings.")
        return

    lines = []
    for s in active:
        r = s.recorder
        name = html.escape(r.recording_prefix or "auto")
        url = html.escape(r.current_url or "—")
        fname = html.escape(r.output_path.parent.name if r.output_path else "—")
        lines.append(
            f"🔴 <b>Session {s.session_num} — {name}</b>\n"
            f"Duration: {r.elapsed_str()} | Size: {r.file_size_str()}\n"
            f"URL: {url}\n"
            f"File: {fname}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ── /history ──────────────────────────────────────────────────────────────────

_history_state: dict[int, dict] = {}
# {user_id: {"folders": [Path,...], "page": int, "selected_folder": Path|None, "selected_file": Path|None}}

_RT_PAGE_SIZE = 10
_MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}


def _get_recording_folders() -> list[Path]:
    if not RECORDINGS_DIR.exists():
        return []
    folders = [p for p in RECORDINGS_DIR.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return folders


def _read_metadata(folder: Path) -> dict:
    try:
        p = folder / ".metadata.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _format_duration(seconds) -> str:
    if not seconds or int(seconds) <= 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _folder_display_size(folder: Path) -> str:
    try:
        total = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        if total >= 1_073_741_824:
            return f"{total / 1_073_741_824:.1f}GB"
        if total >= 1_048_576:
            return f"{total / 1_048_576:.0f}MB"
        return f"{total / 1024:.0f}KB"
    except Exception:
        return "?"


def _file_size_str(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size >= 1_073_741_824:
            return f"{size / 1_073_741_824:.1f} GB"
        if size >= 1_048_576:
            return f"{size / 1_048_576:.0f} MB"
        return f"{size / 1024:.0f} KB"
    except Exception:
        return "?"


def _find_media_files(folder: Path) -> list[Path]:
    try:
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in _MEDIA_EXTS]
        files.sort(key=lambda f: f.stat().st_size, reverse=True)
        return files
    except Exception:
        return []


def _mid_truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    half = (max_len - 1) // 2
    return s[:half] + "…" + s[-(max_len - half - 1):]



async def _summary_ctx_timeout(user_id: int, bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(3600)  # 1 hour
    if _pending_summary_context.pop(user_id, None) is not None:
        try:
            await bot.edit_message_text(
                "⏱ Waktu habis. Tekan 🤖 AI Summary lagi untuk mencoba kembali.",
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            pass


def _cancel_summary_ctx_timeout(user_id: int) -> None:
    state = _pending_summary_context.get(user_id)
    if not state:
        return
    task = state.get("timeout_task")
    if task and not task.done():
        task.cancel()


async def _history_timeout(user_id: int, bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(100)
    if _history_state.pop(user_id, None) is not None:
        try:
            await bot.edit_message_text(
                "⏱ Session expired. Use /history to start again.",
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            pass


def _reset_history_timeout(user_id: int, bot, chat_id: int, message_id: int) -> None:
    state = _history_state.get(user_id)
    if not state:
        return
    task = state.get("timeout_task")
    if task and not task.done():
        task.cancel()
    state["timeout_task"] = asyncio.create_task(
        _history_timeout(user_id, bot, chat_id, message_id)
    )


def _cancel_history_timeout(user_id: int) -> None:
    state = _history_state.get(user_id)
    if not state:
        return
    task = state.get("timeout_task")
    if task and not task.done():
        task.cancel()


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("Unauthorized.")
        return

    all_folders = _get_recording_folders()
    # Filter to folders that actually contain media files
    folders = [f for f in all_folders if _find_media_files(f)]
    if not folders:
        await update.message.reply_text("No recordings found.")
        return

    _history_state[user.id] = {
        "folders": folders,
        "page": 0,
        "selected_folder": None,
        "selected_file": None,
        "timeout_task": None,
    }
    msg = await update.effective_chat.send_message("Loading…")
    await _show_history_page(user.id, update.effective_chat.id, context.bot, page=0, edit_msg=msg)
    _reset_history_timeout(user.id, context.bot, update.effective_chat.id, msg.message_id)


async def _show_history_page(user_id: int, chat_id: int, bot, page: int, edit_msg) -> None:
    state = _history_state.get(user_id)
    if not state:
        return

    folders = state["folders"]
    total = len(folders)
    start = page * _RT_PAGE_SIZE
    end = min(start + _RT_PAGE_SIZE, total)

    # Build listing rows (plain text, no <pre>)
    rows = []
    for i, idx in enumerate(range(start, end), start=1):
        try:
            folder = folders[idx]
            meta = _read_metadata(folder)
            media = _find_media_files(folder)
            size_str = _folder_display_size(folder)
            dur_str = _format_duration(meta.get("duration_seconds"))
            has_txt = bool(media) and media[0].with_suffix(".txt").exists()
            if has_txt:
                tx_model = meta.get("transcript", {}).get("model", "")
                tx_label = "Medium TC" if "medium" in tx_model else "Large TC" if tx_model else "TC"
                tx_str = f"✅ {tx_label}"
            else:
                tx_str = "❌ No TC"
            name = html.escape(folder.name)
            rows.append(f"<b>{i}.</b> {name}\n    {size_str}  ·  {dur_str}  ·  {tx_str}")
        except Exception:
            rows.append(f"<b>{i}.</b> [read error]")

    # Number buttons
    num_buttons = []
    row = []
    for i, idx in enumerate(range(start, end), start=1):
        row.append(InlineKeyboardButton(str(i), callback_data=f"rt_folder:{idx}"))
        if len(row) == 5:
            num_buttons.append(row)
            row = []
    if row:
        num_buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"rt_page:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"rt_page:{page + 1}"))
    if nav:
        num_buttons.append(nav)
    num_buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rt_cancel")])

    listing = "\n\n".join(rows)
    text = f"📋 Recordings ({start + 1}–{end} of {total}):\n\n{listing}\n\nSelect number:"
    markup = InlineKeyboardMarkup(num_buttons)
    if edit_msg:
        await edit_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")


async def cb_rt_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    page = int(query.data.split(":")[1])

    if user_id not in _history_state:
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    _history_state[user_id]["page"] = page
    await _show_history_page(user_id, query.message.chat_id, context.bot, page=page, edit_msg=query.message)
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def cb_rt_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    idx = int(query.data.split(":")[1])

    state = _history_state.get(user_id)
    if not state or idx >= len(state["folders"]):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    folder = state["folders"][idx]
    state["selected_folder"] = folder
    media_files = _find_media_files(folder)
    page = state["page"]

    if not media_files:
        await query.edit_message_text(
            f"📁 {html.escape(folder.name)}\n\nNo media files found in this folder.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data=f"rt_page:{page}"),
                InlineKeyboardButton("❌ Cancel", callback_data="rt_cancel"),
            ]]),
        )
        return

    if len(media_files) == 1:
        state["selected_file"] = media_files[0]
        await _show_history_actions(query.message, state)
        _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)
        return

    # Multiple media files — let user pick
    buttons = []
    for i, f in enumerate(media_files):
        buttons.append([InlineKeyboardButton(
            f"🎥 {f.name} ({_file_size_str(f)})",
            callback_data=f"rt_file:{idx}:{i}",
        )])
    buttons.append([
        InlineKeyboardButton("◀️ Back", callback_data=f"rt_page:{page}"),
        InlineKeyboardButton("❌ Cancel", callback_data="rt_cancel"),
    ])
    await query.edit_message_text(
        f"📁 {html.escape(folder.name)}\n\nMultiple media files found — select one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def cb_rt_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _, folder_idx_str, file_idx_str = query.data.split(":")

    state = _history_state.get(user_id)
    folder_idx = int(folder_idx_str)
    if not state or folder_idx >= len(state["folders"]):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    folder = state["folders"][folder_idx]
    media_files = _find_media_files(folder)
    file_idx = int(file_idx_str)
    if file_idx >= len(media_files):
        await query.edit_message_text("File not found.")
        return

    state["selected_file"] = media_files[file_idx]
    await _show_history_actions(query.message, state)
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def _show_history_actions(message, state: dict) -> None:
    f = state["selected_file"]
    folder = state["selected_folder"]
    meta = _read_metadata(folder)
    dur_str = _format_duration(meta.get("duration_seconds"))
    has_txt = f.with_suffix(".txt").exists()

    has_summary = (folder / ".summary.txt").exists()

    if has_txt:
        tx_model = meta.get("transcript", {}).get("model", "")
        tx_note = f"📄 Transcript: {tx_model}" if tx_model else "📄 Transcript exists"
        sum_note = "  ·  🤖 Summary ✅" if has_summary else ""
        buttons = [
            [
                InlineKeyboardButton("🔄 ReTC (Medium)", callback_data="rt_transcribe:medium"),
                InlineKeyboardButton("🔄 ReTC (Large)",  callback_data="rt_transcribe:large-v3"),
            ],
            [InlineKeyboardButton("📄 Send Transcript", callback_data="rt_send_transcript")],
        ]
        if summarizer_mod.is_configured():
            if has_summary:
                buttons.append([
                    InlineKeyboardButton("📋 Send Summary",  callback_data="rt_send_summary"),
                    InlineKeyboardButton("🔄 Re-summarize", callback_data="rt_resummarize"),
                ])
            else:
                buttons.append([InlineKeyboardButton("🤖 AI Summary", callback_data="rt_summarize")])
        buttons.append([InlineKeyboardButton("✏️ Rename", callback_data="rt_rename")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rt_cancel")])
    else:
        tx_note = "No transcript yet"
        sum_note = ""
        buttons = [
            [
                InlineKeyboardButton("🎙 Transcribe (Medium)", callback_data="rt_transcribe:medium"),
                InlineKeyboardButton("🎙 Transcribe (Large)",  callback_data="rt_transcribe:large-v3"),
            ],
            [InlineKeyboardButton("✏️ Rename", callback_data="rt_rename")],
            [InlineKeyboardButton("❌ Cancel", callback_data="rt_cancel")],
        ]

    await message.edit_text(
        f"📁 {html.escape(folder.name)}\n"
        f"🎥 {html.escape(f.name)} ({_file_size_str(f)})\n"
        f"⏱ {dur_str}  ·  {tx_note}{sum_note}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_rt_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    model = query.data.split(":")[1]

    _cancel_history_timeout(user_id)
    state = _history_state.pop(user_id, None)
    if not state or not state.get("selected_file"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    mp4_path = str(state["selected_file"])
    session_key = f"retranscribe/{state['selected_folder'].name}"
    await _transcription_queue.put((mp4_path, query.message.chat_id, session_key, model))
    await query.edit_message_text(
        query.message.text + f"\n\n🎙 Transcription ({model}): {_queue_status_str()}"
    )


async def cb_rt_send_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_history_timeout(user_id)
    state = _history_state.pop(user_id, None)
    if not state or not state.get("selected_file"):
        await query.edit_message_text("Session expired.")
        return
    txt_path = state["selected_file"].with_suffix(".txt")
    if not txt_path.exists():
        await query.edit_message_text("Transcript file not found.")
        return
    await query.edit_message_text(query.message.text + "\n\n📤 Sending…")
    try:
        with open(txt_path, "rb") as f:
            await context.bot.send_document(
                query.message.chat_id, document=f, filename=txt_path.name
            )
        if summarizer_mod.is_configured():
            token_sum = secrets.token_hex(8)
            _pending_summarize_txt[token_sum] = str(txt_path)
            await query.edit_message_text(
                query.message.text + "\n\n📄 Sent.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🤖 AI Summary", callback_data=f"summarize_txt:{token_sum}"),
                ]]),
            )
        else:
            await query.edit_message_text(query.message.text + "\n\n📄 Sent.")
    except Exception as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ Failed to send: {e}")


async def cb_rt_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_history_timeout(user_id)
    _history_state.pop(user_id, None)
    await query.edit_message_text("Cancelled.")


_DRAFT_THROTTLE = 0.15   # seconds between sendMessageDraft calls (no documented rate limit,
                          # but throttle slightly to avoid hammering Telegram)


def _md_escape_name(name: str) -> str:
    """Escape folder-name chars that break Telegram Markdown V1 formatting."""
    for ch in r"_*`[":
        name = name.replace(ch, f"\\{ch}")
    return name


async def _send_summary(
    chat_id: int,
    folder_name: str,
    txt_path: Path,
    bot,
    user_context: str | None = None,
) -> None:
    """Generate (or load cached) summary and send it. Shared by all summary handlers.

    - Cached path: sends immediately, no API call.
    - Live path: uses Telegram Bot API sendMessageDraft (Bot API 9.5) to stream
      text as a native typing bubble. Each on_status / on_text_chunk call updates
      the same draft_id, which Telegram animates smoothly. When generation is done,
      send_message finalises it as a real message and the bubble disappears.
    """
    import asyncio as _asyncio
    import random

    summary_path = txt_path.parent / ".summary.txt"
    header = f"🤖 *AI Summary — {_md_escape_name(folder_name)}*\n\n"

    # ── Cached path ──────────────────────────────────────────────────────────
    if not user_context and summary_path.exists():
        summary = summary_path.read_text(encoding="utf-8")
        full_text = header + "_(cached)_\n\n" + summary
        await _deliver_summary(chat_id, full_text, txt_path, bot)
        return

    # ── Live generation via sendMessageDraft ─────────────────────────────────
    # draft_id identifies this streaming session; same id → animated updates
    draft_id = random.randint(1, 2 ** 31 - 1)
    last_draft_ts: float = 0.0

    async def _push_draft(text: str) -> None:
        nonlocal last_draft_ts
        now = _asyncio.get_event_loop().time()
        if now - last_draft_ts < _DRAFT_THROTTLE:
            return
        try:
            await bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                parse_mode="Markdown",
            )
            last_draft_ts = now
        except Exception:
            pass

    async def on_status(status_text: str) -> None:
        await _push_draft(header + status_text)

    async def on_text_chunk(accumulated: str) -> None:
        await _push_draft(header + accumulated)

    # Show initial "starting" bubble right away
    await _push_draft(header + "⏳ Memulai…")

    try:
        summary = await summarizer_mod.summarize(
            str(txt_path), user_context,
            on_status=on_status,
            on_text_chunk=on_text_chunk,
        )
    except Exception:
        # Clear draft bubble on failure with an empty draft (Bot API 10.0+)
        try:
            await bot.send_message_draft(chat_id=chat_id, draft_id=draft_id, text="")
        except Exception:
            pass
        raise

    try:
        summary_path.write_text(summary, encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save summary file: %s", e)

    # Finalise: send_message dismisses the draft bubble and delivers the real message
    full_text = header + summary
    if len(full_text) <= 4096:
        await bot.send_message(chat_id, full_text, parse_mode="Markdown")
    else:
        chunks = [full_text[i:i + 4000] for i in range(0, len(full_text), 4000)]
        for i, chunk in enumerate(chunks):
            if i > 0:
                chunk = f"_(continued…)_\n\n{chunk}"
            await bot.send_message(chat_id, chunk, parse_mode="Markdown")

    # Follow-up: offer to send full transcript
    token_send = secrets.token_hex(8)
    _pending_send_transcript[token_send] = str(txt_path)
    await bot.send_message(
        chat_id,
        "✅ Summary selesai. Ingin kirim transkripnya?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Send Transcript", callback_data=f"send_txt:{token_send}"),
        ]]),
    )


async def _deliver_summary(chat_id: int, full_text: str, txt_path: Path, bot) -> None:
    """Send a pre-built summary string and follow-up transcript button."""
    if len(full_text) <= 4096:
        await bot.send_message(chat_id, full_text, parse_mode="Markdown")
    else:
        chunks = [full_text[i:i + 4000] for i in range(0, len(full_text), 4000)]
        for i, chunk in enumerate(chunks):
            if i > 0:
                chunk = f"_(continued…)_\n\n{chunk}"
            await bot.send_message(chat_id, chunk, parse_mode="Markdown")

    token_send = secrets.token_hex(8)
    _pending_send_transcript[token_send] = str(txt_path)
    await bot.send_message(
        chat_id,
        "✅ Summary selesai. Ingin kirim transkripnya?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Send Transcript", callback_data=f"send_txt:{token_send}"),
        ]]),
    )


_SUMMARY_CTX_PROMPT = (
    "📋 <b>Sebelum membuat ringkasan, berikan informasi awal tentang meeting ini</b> "
    "(opsional, tapi sangat membantu AI):\n\n"
    "• Meeting ini tentang apa?\n"
    "• Siapa saja pesertanya?\n"
    "• Kapan meeting berlangsung?\n"
    "• Singkatan atau istilah khusus yang perlu diketahui?\n\n"
    "Ketik informasi di atas, atau tekan <b>Skip</b> untuk langsung generate."
)


async def _ask_summary_context(user_id: int, txt_path: Path, folder_name: str, chat_id: int, bot) -> None:
    """Pre-process transcript for unknown terms, then send context-input prompt."""
    # Pre-process: scan for context gaps with cheap model (fast, ~1–2s)
    context_gaps: list[str] = []
    try:
        context_gaps = await summarizer_mod.extract_context_gaps(str(txt_path))
    except Exception as e:
        logger.warning("Context gap extraction failed for %s: %s", txt_path, e)

    # Build prompt — prepend detected gaps as clarification questions if any
    if context_gaps:
        bullets = "\n".join(f"• {q}" for q in context_gaps)
        prompt = (
            f"💭 <b>Beberapa hal yang perlu dikonfirmasi dari transkrip:</b>\n{bullets}\n\n"
            "📋 <b>Berikan konteks tentang meeting ini</b> (opsional, tapi sangat membantu AI):\n\n"
            "• Jawab pertanyaan di atas jika kamu tahu\n"
            "• Tambahkan konteks lain yang relevan\n\n"
            "Ketik, atau tekan <b>Skip</b> untuk langsung generate."
        )
    else:
        prompt = _SUMMARY_CTX_PROMPT

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Skip", callback_data="summary_ctx_skip"),
    ]])
    msg = await bot.send_message(chat_id, prompt, parse_mode="HTML", reply_markup=kb)
    state = {
        "txt_path": txt_path,
        "folder_name": folder_name,
        "chat_id": chat_id,
        "prompt_msg_id": msg.message_id,
    }
    _pending_summary_context[user_id] = state
    state["timeout_task"] = asyncio.create_task(
        _summary_ctx_timeout(user_id, bot, chat_id, msg.message_id)
    )


async def cb_rt_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_history_timeout(user_id)
    state = _history_state.pop(user_id, None)
    if not state or not state.get("selected_file"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    txt_path = state["selected_file"].with_suffix(".txt")
    if not txt_path.exists():
        await query.edit_message_text("Transcript not found. Transcribe first.")
        return

    folder_name = state["selected_folder"].name
    await query.edit_message_text(f"📁 <b>{html.escape(folder_name)}</b>", parse_mode="HTML")
    await _ask_summary_context(user_id, txt_path, folder_name, query.message.chat_id, context.bot)


async def cb_rt_resummarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-summarize from /history — clears cached .summary.txt first so Skip also regenerates."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_history_timeout(user_id)
    state = _history_state.pop(user_id, None)
    if not state or not state.get("selected_file"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    txt_path = state["selected_file"].with_suffix(".txt")
    if not txt_path.exists():
        await query.edit_message_text("Transcript not found. Transcribe first.")
        return

    folder_name = state["selected_folder"].name
    # Delete cached summary so Skip also generates a fresh one
    try:
        (state["selected_folder"] / ".summary.txt").unlink(missing_ok=True)
    except Exception:
        pass

    await query.edit_message_text(f"📁 <b>{html.escape(folder_name)}</b>", parse_mode="HTML")
    await _ask_summary_context(user_id, txt_path, folder_name, query.message.chat_id, context.bot)


async def cb_rt_send_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the cached .summary.txt from /history without regenerating."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_history_timeout(user_id)
    state = _history_state.pop(user_id, None)
    if not state or not state.get("selected_folder"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    folder = state["selected_folder"]
    summary_path = folder / ".summary.txt"
    if not summary_path.exists():
        await query.edit_message_text("Summary file not found. Use 🤖 AI Summary to generate one.")
        return

    txt_path = state["selected_file"].with_suffix(".txt")
    folder_name = folder.name
    summary = summary_path.read_text(encoding="utf-8")

    await query.edit_message_text(f"📁 <b>{html.escape(folder_name)}</b>", parse_mode="HTML")

    header = f"🤖 *AI Summary — {_md_escape_name(folder_name)}* _(cached)_\n\n"
    full_text = header + summary
    await _deliver_summary(query.message.chat_id, full_text, txt_path, context.bot)


async def cb_summarize_txt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Token-based AI Summary — used from post-transcription and post-send-transcript buttons."""
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 1)[1]
    txt_path_str = _pending_summarize_txt.pop(token, None)
    if not txt_path_str or not Path(txt_path_str).exists():
        await query.edit_message_text(query.message.text + "\n\n⚠️ File not found.")
        return

    txt_path = Path(txt_path_str)
    folder_name = txt_path.parent.name
    user_id = update.effective_user.id
    await _ask_summary_context(user_id, txt_path, folder_name, query.message.chat_id, context.bot)


async def cb_summary_ctx_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User pressed ⏭ Lewati — generate summary without extra context."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    _cancel_summary_ctx_timeout(user_id)
    state = _pending_summary_context.pop(user_id, None)
    if not state:
        await query.edit_message_text("⚠️ Session tidak ditemukan. Coba lagi.")
        return

    txt_path: Path = state["txt_path"]
    folder_name: str = state["folder_name"]
    chat_id: int = state["chat_id"]

    await query.edit_message_text(f"📁 <b>{html.escape(folder_name)}</b>", parse_mode="HTML")
    try:
        await _send_summary(chat_id, folder_name, txt_path, context.bot)
    except Exception as e:
        logger.error("Summarization failed for %s: %s", txt_path, e)
        await context.bot.send_message(chat_id, f"⚠️ Summarization failed: {e}")


async def _handle_summary_context_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called from msg_handler when user is in summary context input state."""
    user_id = update.effective_user.id
    _cancel_summary_ctx_timeout(user_id)
    state = _pending_summary_context.pop(user_id, None)
    if not state:
        return

    user_ctx = update.message.text.strip()
    txt_path: Path = state["txt_path"]
    folder_name: str = state["folder_name"]
    chat_id: int = state["chat_id"]

    # Remove the Skip button from the prompt message so it can't be clicked late
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=state["prompt_msg_id"],
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        await _send_summary(chat_id, folder_name, txt_path, context.bot, user_context=user_ctx)
    except Exception as e:
        logger.error("Summarization failed for %s: %s", txt_path, e)
        await context.bot.send_message(chat_id, f"⚠️ Summarization failed: {e}")


async def cb_rt_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _history_state.get(user_id)
    if not state or not state.get("selected_folder"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return
    state["sub_state"] = "input_rename"
    state["pending_rename"] = None
    state["rename_msg_id"] = query.message.message_id
    await query.edit_message_text(
        f"📁 {html.escape(state['selected_folder'].name)}\n\n"
        f"Send the new name: (100s timeout)",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Abort", callback_data="rt_rename_abort"),
        ]]),
    )
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def _handle_rename_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = _history_state.get(user_id)
    if not state:
        return

    new_name = update.message.text.strip()
    folder = state["selected_folder"]
    chat_id = update.effective_chat.id
    msg_id = state.get("rename_msg_id")

    if not new_name:
        await update.message.reply_text("Name can't be empty.")
        return
    if len(new_name) > 80:
        await update.message.reply_text("Name too long (max 80 characters).")
        return
    if any(c in _RENAME_FORBIDDEN for c in new_name):
        await update.message.reply_text('Name contains invalid characters: / \\ : * ? " < > |')
        return
    if new_name == folder.name:
        await update.message.reply_text("That's already the current name.")
        return
    if (folder.parent / new_name).exists():
        await update.message.reply_text(
            f"A folder named <b>{html.escape(new_name)}</b> already exists.",
            parse_mode="HTML",
        )
        return

    state["pending_rename"] = new_name
    old_prefix = _extract_file_prefix(folder.name)
    new_prefix = _extract_file_prefix(new_name)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="rt_rename_confirm"),
        InlineKeyboardButton("✏️ Change",  callback_data="rt_rename_change"),
    ], [
        InlineKeyboardButton("❌ Abort", callback_data="rt_rename_abort"),
    ]])
    # Show what files will be renamed if prefix changes
    if old_prefix != new_prefix:
        file_note = f"\n📄 Files: <code>{html.escape(old_prefix)}.*</code> → <code>{html.escape(new_prefix)}.*</code>"
    else:
        file_note = ""
    confirm_text = (
        f"📁 {html.escape(folder.name)}\n"
        f"↪️ Rename to: <b>{html.escape(new_name)}</b>{file_note}\n\n"
        f"Confirm?"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=confirm_text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception:
        msg = await update.message.reply_text(confirm_text, reply_markup=kb, parse_mode="HTML")
        state["rename_msg_id"] = msg.message_id
    _reset_history_timeout(user_id, context.bot, chat_id, state["rename_msg_id"])


async def cb_rt_rename_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _history_state.get(user_id)
    if not state or not state.get("pending_rename"):
        await query.edit_message_text("Session expired. Use /history to start again.")
        return

    new_name = state["pending_rename"]
    folder = state["selected_folder"]
    new_path = folder.parent / new_name

    if new_path.exists():
        await query.edit_message_text(
            f"⚠️ A folder named <b>{html.escape(new_name)}</b> already exists.",
            parse_mode="HTML",
        )
        return

    # Determine file prefix change
    old_prefix = _extract_file_prefix(folder.name)
    new_prefix = _extract_file_prefix(new_name)

    # Rename files with matching stem inside the folder first
    rename_errors = []
    if old_prefix != new_prefix:
        for f in sorted(folder.iterdir()):
            if f.is_file() and not f.name.startswith('.') and f.stem == old_prefix:
                try:
                    f.rename(f.parent / (new_prefix + f.suffix))
                except Exception as e:
                    rename_errors.append(f.name)
                    logger.warning("Could not rename file %s: %s", f, e)

    # Rename the folder itself
    try:
        folder.rename(new_path)
    except Exception as e:
        await query.edit_message_text(f"⚠️ Rename failed: {e}")
        return

    # Update .metadata.json
    meta_path = new_path / ".metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["recording_name"] = new_prefix
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # non-fatal

    # Update state
    state["selected_folder"] = new_path
    if state.get("selected_file"):
        old_file = state["selected_file"]
        state["selected_file"] = new_path / (new_prefix + old_file.suffix)
    state["sub_state"] = None
    state["pending_rename"] = None
    state["folders"] = [f for f in _get_recording_folders() if _find_media_files(f)]

    # Return to actions screen (it will show the updated folder name)
    await _show_history_actions(query.message, state)
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)

    if rename_errors:
        await context.bot.send_message(
            query.message.chat_id,
            f"⚠️ Could not rename: {', '.join(rename_errors)}",
        )


async def cb_rt_rename_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _history_state.get(user_id)
    if not state:
        await query.edit_message_text("Session expired.")
        return
    state["pending_rename"] = None
    await query.edit_message_text(
        f"📁 {html.escape(state['selected_folder'].name)}\n\n"
        f"Send the new name: (100s timeout)",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Abort", callback_data="rt_rename_abort"),
        ]]),
    )
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


async def cb_rt_rename_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _history_state.get(user_id)
    if not state:
        await query.edit_message_text("Session expired. Use /history to start again.")
        return
    state["sub_state"] = None
    state["pending_rename"] = None
    await _show_history_actions(query.message, state)
    _reset_history_timeout(user_id, context.bot, query.message.chat_id, query.message.message_id)


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return

    user_id = update.effective_user.id
    own = len(store.active(user_id))
    total = store.total_active()

    if own:
        await update.message.reply_text(
            f"Your active: {own}. Total across all users: {total}. Use /ongoing for details."
        )
    else:
        await update.message.reply_text(
            f"You have no active recordings. Total across all users: {total}."
        )


# ── main ──────────────────────────────────────────────────────────────────────

_HELP_TEXT = (
    "🎙 <b>Zoomy — Zoom Meeting Recorder</b>\n\n"
    "<b>Commands:</b>\n"
    "/record — Start recording a Zoom meeting\n"
    "/stop — Stop the active recording\n"
    "/peek — Screenshot of the active meeting\n"
    "/schedule — View, reschedule, or cancel scheduled recordings\n"
    "/status — Active count + total across all users\n"
    "/history — Browse recordings; transcribe, rename, or AI summary"
)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(_HELP_TEXT, parse_mode="HTML")


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=context.error)


async def _restore_schedules(bot) -> None:
    """On startup: reload schedules from file, re-spawn tasks, notify about missed ones."""
    global _sched_counter
    entries = _read_schedule_file()
    if not entries:
        return

    now_utc = datetime.now(timezone.utc)
    restored, missed = 0, []

    for entry in entries:
        try:
            dt = datetime.fromisoformat(entry["scheduled_time"])
            delay = (dt - now_utc).total_seconds()
            sched_id = entry["sched_id"]
            user_id  = entry["user_id"]
            chat_id  = entry["chat_id"]
            p        = entry["pending"]
            desc     = entry.get("description", "auto-name")
            dt_wib   = dt.astimezone(WIB)

            _sched_counter = max(_sched_counter, sched_id)

            if delay <= 0:
                missed.append(entry)
                continue

            task = asyncio.create_task(
                _run_scheduled(sched_id, user_id, chat_id, bot, p, delay, dt_wib)
            )
            _scheduled.setdefault(user_id, []).append(ScheduledRecording(
                sched_id=sched_id,
                user_id=user_id,
                chat_id=chat_id,
                scheduled_time=dt_wib,
                description=desc,
                task=task,
            ))
            restored += 1
        except Exception as e:
            logger.warning("Failed to restore schedule entry %s: %s", entry, e)

    # Notify users about schedules that were missed during downtime
    for entry in missed:
        _remove_from_schedule_file(entry["sched_id"])
        try:
            dt_wib = datetime.fromisoformat(entry["scheduled_time"]).astimezone(WIB)
            time_str = dt_wib.strftime("%a %d %b at %H:%M WIB")
            await bot.send_message(
                entry["chat_id"],
                f"⚠️ Missed scheduled recording (bot was restarted)\n"
                f"🕐 {time_str}\n"
                f"📝 {html.escape(entry.get('description', 'auto-name'))}",
            )
        except Exception as e:
            logger.warning("Could not notify missed schedule %d: %s", entry.get("sched_id"), e)

    if restored:
        logger.info("Restored %d scheduled recording(s) from file", restored)
    if missed:
        logger.info("Notified %d missed scheduled recording(s)", len(missed))


# ── Reschedule helpers ────────────────────────────────────────────────────────

async def _reschedule_timeout(user_id: int, bot, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(100)
    if _reschedule_state.pop(user_id, None) is not None:
        try:
            await bot.edit_message_text(
                "⏱ Reschedule timed out.",
                chat_id=chat_id, message_id=message_id,
            )
        except Exception:
            pass


def _reset_reschedule_timeout(user_id: int, bot, chat_id: int, message_id: int) -> None:
    state = _reschedule_state.get(user_id)
    if not state:
        return
    t = state.get("timeout_task")
    if t and not t.done():
        t.cancel()
    state["timeout_task"] = asyncio.create_task(
        _reschedule_timeout(user_id, bot, chat_id, message_id)
    )


def _cancel_reschedule_timeout(user_id: int) -> None:
    state = _reschedule_state.get(user_id)
    if not state:
        return
    t = state.get("timeout_task")
    if t and not t.done():
        t.cancel()


def _spawn_transcription_worker(bot) -> None:
    """Create the transcription worker task with an auto-restart done-callback.

    If the task exits for any reason other than explicit cancellation (bot
    shutdown), it is automatically restarted.  Cancelled tasks are left alone
    so a clean shutdown doesn't loop forever.
    """
    task = asyncio.create_task(_transcription_worker(bot))

    def _on_worker_done(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.info("Transcription worker cancelled — not restarting")
            return
        exc = t.exception()
        if exc:
            logger.error(
                "Transcription worker crashed — restarting",
                exc_info=exc,
            )
        else:
            logger.warning("Transcription worker exited cleanly — restarting")
        _spawn_transcription_worker(bot)

    task.add_done_callback(_on_worker_done)


async def _post_init(application: Application) -> None:
    _spawn_transcription_worker(application.bot)
    await _restore_schedules(application.bot)
    _cleanup_stale_wavs()


def _cleanup_stale_wavs() -> None:
    """Delete leftover .wav files in recordings that weren't cleaned up (e.g. after a crash)."""
    try:
        count = 0
        for wav in RECORDINGS_DIR.rglob("*.wav"):
            try:
                wav.unlink()
                count += 1
                logger.info("Removed stale WAV: %s", wav)
            except Exception as e:
                logger.warning("Could not remove stale WAV %s: %s", wav, e)
        if count:
            logger.info("Startup cleanup: removed %d stale WAV file(s)", count)
    except Exception as e:
        logger.warning("Stale WAV cleanup failed: %s", e)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_error_handler(_error_handler)
    app.add_handler(CommandHandler("record",        cmd_record))
    app.add_handler(CommandHandler("stop",          cmd_stop))
    app.add_handler(CommandHandler("peek",          cmd_peek))
    app.add_handler(CommandHandler("schedule",      cmd_schedule))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("history",       cmd_history))
    app.add_handler(CallbackQueryHandler(cb_rt_page,             pattern=r"^rt_page:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rt_folder,           pattern=r"^rt_folder:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rt_file,             pattern=r"^rt_file:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rt_transcribe,       pattern=r"^rt_transcribe:"))
    app.add_handler(CallbackQueryHandler(cb_rt_send_transcript,  pattern="^rt_send_transcript$"))
    app.add_handler(CallbackQueryHandler(cb_rt_summarize,        pattern="^rt_summarize$"))
    app.add_handler(CallbackQueryHandler(cb_rt_resummarize,      pattern="^rt_resummarize$"))
    app.add_handler(CallbackQueryHandler(cb_rt_send_summary,     pattern="^rt_send_summary$"))
    app.add_handler(CallbackQueryHandler(cb_rt_cancel,           pattern="^rt_cancel$"))
    app.add_handler(CallbackQueryHandler(cb_rt_rename,           pattern="^rt_rename$"))
    app.add_handler(CallbackQueryHandler(cb_rt_rename_confirm,   pattern="^rt_rename_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_rt_rename_change,    pattern="^rt_rename_change$"))
    app.add_handler(CallbackQueryHandler(cb_rt_rename_abort,     pattern="^rt_rename_abort$"))
    app.add_handler(CallbackQueryHandler(cb_send_txt,            pattern=r"^send_txt:"))
    app.add_handler(CallbackQueryHandler(cb_summarize_txt,       pattern=r"^summarize_txt:"))
    app.add_handler(CallbackQueryHandler(cb_summary_ctx_skip,    pattern="^summary_ctx_skip$"))
    app.add_handler(CallbackQueryHandler(cb_start_now,           pattern="^start_now$"))
    app.add_handler(CallbackQueryHandler(cb_schedule_later,      pattern="^schedule_later$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_schedule,     pattern=r"^cancel_schedule_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_reschedule_start,    pattern=r"^reschedule_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reschedule_confirm,  pattern="^reschedule_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_reschedule_change,   pattern="^reschedule_change$"))
    app.add_handler(CallbackQueryHandler(cb_reschedule_abort,    pattern="^reschedule_abort$"))
    app.add_handler(CallbackQueryHandler(cb_confirm_new_session, pattern="^confirm_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_new_session,  pattern="^cancel_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_use_default,         pattern="^use_default$"))
    app.add_handler(CallbackQueryHandler(cb_change_name,         pattern="^change_name$"))
    app.add_handler(CallbackQueryHandler(cb_skip_pwd,             pattern="^skip_pwd$"))
    app.add_handler(CallbackQueryHandler(cb_skip_rec_name,       pattern="^skip_rec_name$"))
    app.add_handler(CallbackQueryHandler(cb_peek_session,        pattern=r"^peek_session_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_peek_all,            pattern="^peek_all_sessions$"))
    app.add_handler(CallbackQueryHandler(cb_stop_session,        pattern=r"^stop_session_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_stop_all,            pattern="^stop_all_sessions$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_stop,         pattern="^cancel_stop$"))
    app.add_handler(CallbackQueryHandler(cb_silence_snooze,      pattern=r"^silence_snooze:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_silence_wait,        pattern=r"^silence_wait:\d+$"))
    app.add_handler(CallbackQueryHandler(_handle_transcribe_callback, pattern=r"^(transcribe_medium|transcribe_large|skip):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))  # catch-all — must be last
    logger.info("Zoomy bot started (@baboonrecord_bot)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
