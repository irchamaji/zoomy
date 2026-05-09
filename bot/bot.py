import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import logging
import logging.handlers
import os
import subprocess
from pathlib import Path
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

from recorder import ZoomRecorder, _display_pool
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
DEFAULT_GUEST_NAME = os.environ.get("GUEST_NAME", "Zoomy")
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
_pending_transcriptions: dict[str, str] = {}  # session_key → mp4_path

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
    while True:
        mp4_path, chat_id, session_key = await _transcription_queue.get()
        await bot.send_message(chat_id, f"Transcribing session {session_key}…")
        try:
            txt, srt = await transcriber_mod.transcribe(mp4_path, WHISPER_MODEL, WHISPER_LANGUAGE)
            name = Path(txt).stem
            await bot.send_message(
                chat_id,
                f"Transcript saved — Session {session_key}\n  {name}.txt\n  {name}.srt",
            )
        except Exception as e:
            await bot.send_message(chat_id, f"Transcription failed (session {session_key}): {e}")
        finally:
            _transcription_queue.task_done()


async def _transcribe_timeout(session_key: str, mp4_path: str, chat_id: int, bot) -> None:
    await asyncio.sleep(100)
    if session_key in _pending_transcriptions:
        _pending_transcriptions.pop(session_key)
        was_empty = _transcription_queue.empty()
        await _transcription_queue.put((mp4_path, chat_id, session_key))
        status = "Starting now…" if was_empty else f"Queued (position {_transcription_queue.qsize()})"
        await bot.send_message(chat_id, f"Auto-transcribing session {session_key}. {status}")


async def _handle_transcribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, session_key = query.data.split(":", 1)

    if action == "transcribe":
        mp4_path = _pending_transcriptions.pop(session_key, None)
        if not mp4_path:
            await query.edit_message_text(query.message.text + "\n\n⚠️ Request expired.")
            return
        was_empty = _transcription_queue.empty()
        await _transcription_queue.put((mp4_path, query.message.chat_id, session_key))
        status = "Starting now…" if was_empty else f"Queued (position {_transcription_queue.qsize()})"
        await query.edit_message_text(query.message.text + f"\n\nTranscription: {status}")

    elif action == "skip":
        _pending_transcriptions.pop(session_key, None)
        await query.edit_message_text(query.message.text + "\n\nTranscription skipped.")


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

    if url and any(s.url == url for s in active):
        await update.message.reply_text("Already recording this meeting.")
        return

    store.set_pending(user_id, {
        "url": url,
        "guest_name": DEFAULT_GUEST_NAME,
        "resolution": "1080p",
        "state": None,
        "timeout_task": None,
    })

    # No URL provided — ask for it
    if not url:
        store.update_pending(user_id, state="input_url")
        await update.message.reply_text("Send me the Zoom URL: (100s timeout)")
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

    await _send_name_keyboard(user_id, context.bot)


async def _url_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_url":
        store.pop_pending(user_id)
        await bot.send_message(user_id, "Timed out — no URL received. Send /record to try again.")


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
        await _ask_resolution(user_id, bot)


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
        await _ask_resolution(user_id, bot)


async def cb_skip_rec_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if store.get_pending(user_id).get("state") != "input_rec_name":
        return

    _cancel_timeout(user_id)
    store.update_pending(user_id, state=None)
    await query.edit_message_text("Using auto-name.")
    await _ask_resolution(user_id, context.bot)


# ── Message router ────────────────────────────────────────────────────────────

async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    state = store.get_pending(user_id).get("state")

    if state == "input_url":
        url = update.message.text.strip()
        _cancel_timeout(user_id)
        active = store.active(user_id)
        if any(s.url == url for s in active):
            store.pop_pending(user_id)
            await update.message.reply_text("Already recording this meeting.")
            return
        store.update_pending(user_id, url=url, state=None)
        await update.message.reply_text(f"URL received.")
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
        await _ask_resolution(user_id, context.bot)

    elif state == "input_schedule_time":
        text = update.message.text.strip()
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
            f"👤 Bot name: {html.escape(p.get('guest_name', DEFAULT_GUEST_NAME))}\n"
            f"📺 Resolution: {p.get('resolution', '1080p')}",
            reply_markup=kb,
        )


# ── Resolution step ───────────────────────────────────────────────────────────

async def _ask_resolution(user_id: int, bot) -> None:
    _cancel_timeout(user_id)
    store.update_pending(user_id, state="waiting_resolution")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("360p", callback_data="resolution_360p"),
        InlineKeyboardButton("720p", callback_data="resolution_720p"),
        InlineKeyboardButton("1080p", callback_data="resolution_1080p"),
    ]])
    await bot.send_message(user_id, "Select recording resolution: (auto 1080p in 100s)", reply_markup=kb)
    task = asyncio.create_task(_resolution_timeout(user_id, bot))
    store.update_pending(user_id, timeout_task=task)


async def _resolution_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "waiting_resolution":
        store.update_pending(user_id, resolution="1080p", state=None)
        await bot.send_message(user_id, "No response — using 1080p.")
        await _ask_start_or_schedule(user_id, bot)


async def cb_resolution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return

    _cancel_timeout(user_id)
    resolution = query.data.split("_", 1)[1]
    store.update_pending(user_id, resolution=resolution, state=None)
    await query.edit_message_text(f"Resolution: {resolution}")
    await _ask_start_or_schedule(user_id, context.bot)


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

    async def on_stopped(filename: str, duration: str, size: str, auto_ended: bool = False) -> None:
        folder = Path(filename).parent.name
        reason = "Meeting ended by host" if auto_ended else "Stopped manually"
        session_key = str(session_num)
        _pending_transcriptions[session_key] = filename
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Transcribe", callback_data=f"transcribe:{session_key}"),
            InlineKeyboardButton("Skip",       callback_data=f"skip:{session_key}"),
        ]])
        await bot.send_message(
            chat_id,
            f"Recording saved — Session {session_num}\n"
            f"Folder: {folder}\nDuration: {duration}\nSize: {size}\nReason: {reason}\n\n"
            f"Transcribe with Whisper? (auto-transcribing in 100s)",
            reply_markup=keyboard,
        )
        asyncio.create_task(_transcribe_timeout(session_key, filename, chat_id, bot))

    async def on_error(msg: str) -> None:
        await bot.send_message(chat_id, f"Session {session_num} error: {msg}")

    async def on_dialog(dialog_text: str) -> None:
        short = dialog_text[:300].strip()
        await bot.send_message(
            chat_id,
            f"ℹ️ <b>Session {session_num} — dialog dismissed:</b>\n<i>{html.escape(short)}</i>",
            parse_mode="HTML",
        )

    recorder = ZoomRecorder(
        display=display_str,
        sink=sink_monitor,
        guest_name=guest_name,
        recording_prefix=prefix,
        resolution=resolution,
        on_started=on_started,
        on_stopped=on_stopped,
        on_error=on_error,
        on_dialog=on_dialog,
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
        buttons.append([InlineKeyboardButton(
            f"❌ Cancel #{s.sched_id} ({time_str})",
            callback_data=f"cancel_schedule_{s.sched_id}",
        )])
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
        caption = (
            f"📸 Session {session.session_num} — {name}\n"
            f"⏱ {session.recorder.elapsed_str()} | 💾 {session.recorder.file_size_str()}"
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


async def _post_init(application: Application) -> None:
    asyncio.create_task(_transcription_worker(application.bot))
    await _restore_schedules(application.bot)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_error_handler(_error_handler)
    app.add_handler(CommandHandler("record", cmd_record))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("peek", cmd_peek))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ongoing", cmd_ongoing))
    app.add_handler(CallbackQueryHandler(cb_start_now,           pattern="^start_now$"))
    app.add_handler(CallbackQueryHandler(cb_schedule_later,      pattern="^schedule_later$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_schedule,     pattern=r"^cancel_schedule_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_confirm_new_session, pattern="^confirm_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_new_session,  pattern="^cancel_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_use_default,         pattern="^use_default$"))
    app.add_handler(CallbackQueryHandler(cb_change_name,         pattern="^change_name$"))
    app.add_handler(CallbackQueryHandler(cb_skip_rec_name,       pattern="^skip_rec_name$"))
    app.add_handler(CallbackQueryHandler(cb_resolution,          pattern="^resolution_(360p|720p|1080p)$"))
    app.add_handler(CallbackQueryHandler(cb_peek_session,        pattern=r"^peek_session_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_peek_all,            pattern="^peek_all_sessions$"))
    app.add_handler(CallbackQueryHandler(cb_stop_session,        pattern=r"^stop_session_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_stop_all,            pattern="^stop_all_sessions$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_stop,              pattern="^cancel_stop$"))
    app.add_handler(CallbackQueryHandler(_handle_transcribe_callback, pattern=r"^(transcribe|skip):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Zoomy bot started (@baboonrecord_bot)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
