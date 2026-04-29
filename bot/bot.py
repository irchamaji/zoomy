import asyncio
import html
import logging
import logging.handlers
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

store = SessionStore()


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

    if not context.args:
        await update.message.reply_text("Usage: /record <zoom_url>")
        return

    url = context.args[0]
    active = store.active(user_id)

    if any(s.url == url for s in active):
        await update.message.reply_text("Already recording this meeting.")
        return

    store.set_pending(user_id, {
        "url": url,
        "guest_name": DEFAULT_GUEST_NAME,
        "prefix": None,
        "resolution": "1080p",
        "state": None,
        "timeout_task": None,
    })

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


async def _send_name_keyboard(user_id: int, bot) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Change Bot Name", callback_data="change_name"),
        InlineKeyboardButton(f"Use {DEFAULT_GUEST_NAME}", callback_data="use_default"),
    ]])
    await bot.send_message(
        user_id,
        f"Bot will join as '{DEFAULT_GUEST_NAME}'. Change for this session?",
        reply_markup=kb,
    )


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

    await query.edit_message_text(f"Using name: {DEFAULT_GUEST_NAME}")
    await _ask_recording_name(user_id, context.bot)


async def cb_change_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired.")
        return

    store.update_pending(user_id, state="input_name")
    await query.edit_message_text("Type the bot name for this session:")


# ── Recording name step ───────────────────────────────────────────────────────

async def _ask_recording_name(user_id: int, bot) -> None:
    store.update_pending(user_id, state="input_rec_name")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="skip_rec_name")]])
    await bot.send_message(
        user_id,
        "Recording name? Reply within 100s or press Skip.",
        reply_markup=kb,
    )
    task = asyncio.create_task(_rec_name_timeout(user_id, bot))
    store.update_pending(user_id, timeout_task=task)


async def _rec_name_timeout(user_id: int, bot) -> None:
    await asyncio.sleep(100)
    if store.get_pending(user_id).get("state") == "input_rec_name":
        store.update_pending(user_id, state=None)
        await bot.send_message(user_id, "Timeout — using auto-name.")
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

    if state == "input_name":
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


# ── Resolution step ───────────────────────────────────────────────────────────

async def _ask_resolution(user_id: int, bot) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("360p", callback_data="resolution_360p"),
        InlineKeyboardButton("720p", callback_data="resolution_720p"),
        InlineKeyboardButton("1080p", callback_data="resolution_1080p"),
    ]])
    await bot.send_message(user_id, "Select recording resolution:", reply_markup=kb)


async def cb_resolution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if not store.has_pending(user_id):
        await query.edit_message_text("Session expired. Start again with /record.")
        return

    resolution = query.data.split("_", 1)[1]
    store.update_pending(user_id, resolution=resolution)
    await query.edit_message_text(f"Resolution: {resolution}")
    await _start_recording(user_id, update.effective_chat.id, context.bot)


def _cancel_timeout(user_id: int) -> None:
    task = store.get_pending(user_id).get("timeout_task")
    if task and not task.done():
        task.cancel()


# ── Start recording ───────────────────────────────────────────────────────────

async def _start_recording(user_id: int, chat_id: int, bot) -> None:
    p = store.pop_pending(user_id)
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
        "Session %d starting — user=%d url=%s guest=%s prefix=%s res=%s display=%s",
        session_num, user_id, url, guest_name, prefix or "auto", resolution, display_str,
    )

    async def on_started(filename: str) -> None:
        await bot.send_message(
            chat_id,
            f"Recording started — Session {session_num}\nFile: {filename}",
        )

    async def on_stopped(filename: str, duration: str, size: str, auto_ended: bool = False) -> None:
        reason = "Meeting ended by host" if auto_ended else "Stopped manually"
        await bot.send_message(
            chat_id,
            f"Recording saved — Session {session_num}\n"
            f"File: {filename}\nDuration: {duration}\nSize: {size}\nReason: {reason}",
        )

    async def on_error(msg: str) -> None:
        await bot.send_message(chat_id, f"Session {session_num} error: {msg}")

    recorder = ZoomRecorder(
        display=display_str,
        sink=sink_monitor,
        guest_name=guest_name,
        recording_prefix=prefix,
        resolution=resolution,
        on_started=on_started,
        on_stopped=on_stopped,
        on_error=on_error,
    )

    async def run() -> None:
        try:
            await recorder.record(url)
        finally:
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
        fname = html.escape(r.output_path.name if r.output_path else "—")
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


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(_error_handler)
    app.add_handler(CommandHandler("record", cmd_record))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ongoing", cmd_ongoing))
    app.add_handler(CallbackQueryHandler(cb_confirm_new_session, pattern="^confirm_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_new_session,  pattern="^cancel_new_session$"))
    app.add_handler(CallbackQueryHandler(cb_use_default,         pattern="^use_default$"))
    app.add_handler(CallbackQueryHandler(cb_change_name,         pattern="^change_name$"))
    app.add_handler(CallbackQueryHandler(cb_skip_rec_name,       pattern="^skip_rec_name$"))
    app.add_handler(CallbackQueryHandler(cb_resolution,          pattern="^resolution_(360p|720p|1080p)$"))
    app.add_handler(CallbackQueryHandler(cb_stop_session,        pattern=r"^stop_session_(\d+)$"))
    app.add_handler(CallbackQueryHandler(cb_stop_all,            pattern="^stop_all_sessions$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_stop,         pattern="^cancel_stop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Zoomy bot started (@baboonrecord_bot)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
