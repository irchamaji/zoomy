"""Offline regression tests: Telegram calls are mocked; no meeting is joined.

Run: python -m unittest discover -s tests -v
Requires bot requirements except faster-whisper (audio inference is not exercised).
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bot'))
with tempfile.TemporaryDirectory() as log_dir:
    spec = importlib.util.spec_from_file_location('zoomy_bot', ROOT / 'bot' / 'bot.py')
    bot = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bot
    with patch.dict(os.environ, TELEGRAM_TOKEN='123:offline-test', RECORDINGS_DIR=log_dir), \
         patch.dict(sys.modules, transcriber=types.ModuleType('transcriber')):
        spec.loader.exec_module(bot)


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.store = bot.SessionStore()
        for state in (bot._history_state, bot._reschedule_state, bot._pending_summary_context,
                      bot._summary_conversations, bot._scheduled):
            state.clear()
        bot.AUTHORIZED_IDS = {7}
        self.message = types.SimpleNamespace(
            message_id=100, chat_id=7, text='', reply_text=AsyncMock(), edit_text=AsyncMock())
        self.query = types.SimpleNamespace(
            message=self.message, data='start_now', answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=7, username='tester', first_name='Tester'),
            effective_chat=types.SimpleNamespace(id=7, send_message=AsyncMock(return_value=self.message)),
            message=self.message, callback_query=self.query)
        self.context = types.SimpleNamespace(args=[], bot=types.SimpleNamespace(
            send_message=AsyncMock(return_value=self.message), edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock()))

    async def asyncTearDown(self):
        for mapping in (bot.store._pending, bot._history_state, bot._reschedule_state,
                        bot._pending_summary_context, bot._summary_conversations):
            for state in mapping.values():
                task = state.get('timeout_task')
                if task:
                    task.cancel()
        await asyncio.sleep(0)

    async def test_spaced_inline_meeting_id(self):
        self.context.args = ['978', '5600', '7427']
        await bot.cmd_record(self.update, self.context)
        self.assertEqual(bot.store.get_pending(7)['url'], 'https://zoom.us/j/97856007427')
        self.assertEqual(bot.store.get_pending(7)['state'], 'input_pwd')

    async def test_complete_setup_reaches_launch_once(self):
        self.context.args = ['97856007427']
        await bot.cmd_record(self.update, self.context)
        for data, handler in (
            ('skip_pwd', bot.cb_skip_pwd),
            ('use_default', bot.cb_use_default),
            ('skip_rec_name', bot.cb_skip_rec_name),
        ):
            self.query.data = data
            await bot._guard_callback(self.update, self.context)
            await handler(self.update, self.context)
        self.query.data = 'start_now'
        with patch.object(bot, '_launch_recording', new=AsyncMock()) as launch:
            await bot._guard_callback(self.update, self.context)
            await bot.cb_start_now(self.update, self.context)
            launch.assert_awaited_once()
            self.assertEqual(launch.call_args.args[3]['url'], 'https://zoom.us/j/97856007427')
            with self.assertRaises(bot.ApplicationHandlerStop):
                await bot._guard_callback(self.update, self.context)
        self.assertFalse(bot.store.has_pending(7))

    async def test_old_button_from_different_chat_is_rejected(self):
        bot._history_state[7] = dict(message_id=100, chat_id=-999)
        self.query.data = 'rt_page:0'
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)

    async def test_old_setup_button_cannot_start_new_setup(self):
        bot.store.set_pending(7, dict(state='waiting_start', start_msg_id=101))
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)
        self.assertTrue(bot.store.has_pending(7))
        self.query.answer.assert_awaited_once()

    async def test_current_setup_button_passes_guard(self):
        bot.store.set_pending(7, dict(state='waiting_start', start_msg_id=100))
        await bot._guard_callback(self.update, self.context)
        self.query.answer.assert_not_awaited()

    async def test_wrong_step_button_is_rejected(self):
        bot.store.set_pending(7, dict(state='input_pwd', start_msg_id=100))
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)

    async def test_callback_authorization(self):
        self.update.effective_user.id = 8
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)
        self.query.answer.assert_awaited_with('Unauthorized.', show_alert=True)

    async def test_old_history_delete_cannot_target_new_selection(self):
        bot._history_state[7] = dict(message_id=101, chat_id=7, selected_folder=Path('/unused'))
        self.query.data = 'rt_delete_confirm'
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)

    async def test_delete_requires_confirmation_for_same_folder(self):
        bot._history_state[7] = dict(selected_folder=Path('/unused'))
        with patch.object(bot.shutil, 'rmtree') as remove:
            await bot.cb_rt_delete_confirm(self.update, self.context)
            remove.assert_not_called()

    async def test_cancel_preserves_recordings_and_schedules(self):
        task = asyncio.create_task(asyncio.sleep(3600))
        session = types.SimpleNamespace(recorder=types.SimpleNamespace(is_recording=True))
        bot.store._sessions[7] = [session]
        schedule = object()
        bot._scheduled[7] = [schedule]
        bot.store.set_pending(7, dict(state='input_pwd', pwd_msg_id=100, timeout_task=task))
        bot._summary_conversations[7] = dict(chat_id=7)
        await bot.cmd_cancel(self.update, self.context)
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())
        self.assertFalse(bot.store.has_pending(7))
        self.assertNotIn(7, bot._summary_conversations)
        self.assertEqual(bot.store.active(7), [session])
        self.assertEqual(bot._scheduled[7], [schedule])

    async def test_record_does_not_steal_ai_input(self):
        bot._summary_conversations[7] = dict(chat_id=7)
        self.context.args = ['97856007427']
        await bot.cmd_record(self.update, self.context)
        self.assertFalse(bot.store.has_pending(7))
        self.assertIn('/cancel', self.message.reply_text.call_args.args[0])

    async def test_summary_button_does_not_steal_record_input(self):
        bot.store.set_pending(7, dict(state='input_pwd'))
        self.query.data = 'summarize_txt:token'
        with self.assertRaises(bot.ApplicationHandlerStop):
            await bot._guard_callback(self.update, self.context)

    async def test_custom_bot_name_has_timeout(self):
        bot.store.set_pending(7, dict(state='waiting_name', name_msg_id=100))
        await bot.cb_change_name(self.update, self.context)
        state = bot.store.get_pending(7)
        self.assertEqual(state['state'], 'input_name')
        self.assertFalse(state['timeout_task'].done())

    async def test_name_timeout_continues_without_cancelling_itself(self):
        bot.store.set_pending(7, dict(state='input_name', guest_name=bot.DEFAULT_GUEST_NAME))
        bot.store.update_pending(7, timeout_task=asyncio.current_task())
        with patch.object(bot.asyncio, 'sleep', new=AsyncMock()):
            await bot._name_timeout(7, self.context.bot)
        self.assertEqual(bot.store.get_pending(7)['state'], 'input_rec_name')

    async def test_review_does_not_expose_passcode(self):
        bot.store.set_pending(7, dict(url='https://zoom.us/j/97856007427?pwd=secret',
                                     platform='zoom', guest_name='Helper', prefix='Daily'))
        await bot._ask_start_or_schedule(7, self.context.bot)
        text = self.context.bot.send_message.call_args.args[1]
        self.assertIn('97856007427', text)
        self.assertIn('Daily', text)
        self.assertIn('Included', text)
        self.assertNotIn('secret', text)

    async def test_history_search_cancels_old_timeout(self):
        old = asyncio.create_task(asyncio.sleep(3600))
        bot._history_state[7] = dict(timeout_task=old)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('Daily_Team', 'Weekly'):
                folder = root / name
                folder.mkdir()
                (folder / 'recording.mp4').write_bytes(b'test')
            self.context.args = ['daily']
            with patch.object(bot, 'RECORDINGS_DIR', root):
                await bot.cmd_history(self.update, self.context)
            self.assertEqual([f.name for f in bot._history_state[7]['folders']], ['Daily_Team'])
        await asyncio.sleep(0)
        self.assertTrue(old.cancelled())
        self.assertEqual(bot._history_state[7]['message_id'], 100)

    async def test_unmatched_text_gets_recovery_instructions(self):
        self.message.text = 'hello'
        await bot.msg_handler(self.update, self.context)
        self.assertIn('/record', self.message.reply_text.call_args.args[0])

    async def test_history_actions_have_back_button_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            media = folder / 'meeting.mp4'
            media.write_bytes(b'test')
            state = dict(selected_folder=folder, selected_file=media, page=2)
            await bot._show_history_actions(self.message, state)
            kwargs = self.message.edit_text.call_args.kwargs
            callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertIn('rt_page:2', callbacks)
            self.assertEqual(kwargs['parse_mode'], 'HTML')

    async def test_concurrent_confirmation_is_expiring_state(self):
        bot.store._sessions[7] = [types.SimpleNamespace(
            session_num=1, url='https://zoom.us/j/97856007427',
            recorder=types.SimpleNamespace(is_recording=True, recording_prefix='Daily'))]
        self.message.reply_text.return_value = self.message
        self.context.args = ['97856007427']
        await bot.cmd_record(self.update, self.context)
        state = bot.store.get_pending(7)
        self.assertEqual(state['state'], 'confirm_session')
        self.assertEqual(state['confirm_msg_id'], 100)
        self.assertFalse(state['timeout_task'].done())

    async def test_help_and_ongoing_are_registered(self):
        app = Mock()
        builder = Mock()
        builder.token.return_value.post_init.return_value.build.return_value = app
        with patch.object(bot.Application, 'builder', return_value=builder):
            bot.main()
        commands = set()
        for call in app.add_handler.call_args_list:
            handler = call.args[0]
            if isinstance(handler, bot.CommandHandler):
                commands.update(handler.commands)
        self.assertTrue({'start', 'help', 'cancel', 'ongoing'} <= commands)

    async def test_youtube_upload_success_reports_link_and_deletion_date(self):
        bot._pending_youtube_retry.clear()
        marker = {'url': 'https://youtu.be/abc', 'privacy': 'unlisted', 'status': 'processed',
                  'uploaded_at': '2026-10-28T00:00:00+00:00'}
        with patch.object(bot.youtube_mod, 'upload_recording', AsyncMock(return_value=marker)):
            await bot._youtube_upload_task(self.context.bot, 7, Path('/rec/Meet_1/Meet_1.mp4'), 'Session 1')
        text = self.context.bot.send_message.call_args.args[1]
        self.assertIn('https://youtu.be/abc', text)
        self.assertIn('04 Nov 2026', text)  # upload date + 7 days
        self.assertEqual(bot._pending_youtube_retry, {})

    async def test_youtube_upload_failure_offers_retry_and_promises_no_delete(self):
        bot._pending_youtube_retry.clear()
        with patch.object(bot.youtube_mod, 'upload_recording', AsyncMock(side_effect=RuntimeError('boom'))):
            await bot._youtube_upload_task(self.context.bot, 7, Path('/rec/Meet_1/Meet_1.mp4'), 'Session 1')
        call = self.context.bot.send_message.call_args
        self.assertIn('NOT be auto-deleted', call.args[1])
        button = call.kwargs['reply_markup'].inline_keyboard[0][0]
        token = button.callback_data.split(':', 1)[1]
        self.assertEqual(bot._pending_youtube_retry[token], '/rec/Meet_1/Meet_1.mp4')

    async def test_youtube_retry_callback_is_registered_and_single_use(self):
        app = Mock()
        builder = Mock()
        builder.token.return_value.post_init.return_value.build.return_value = app
        with patch.object(bot.Application, 'builder', return_value=builder):
            bot.main()
        patterns = [c.args[0].pattern.pattern for c in app.add_handler.call_args_list
                    if isinstance(c.args[0], bot.CallbackQueryHandler) and c.args[0].pattern]
        self.assertIn(r'^yt_retry:', patterns)

        self.query.data = 'yt_retry:gone'
        await bot.cb_yt_retry(self.update, self.context)
        self.query.answer.assert_awaited_once()
        self.assertTrue(self.query.answer.call_args.kwargs.get('show_alert'))


if __name__ == '__main__':
    unittest.main()
