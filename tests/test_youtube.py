"""Retention and upload-bookkeeping tests for bot/youtube.py. YouTube is mocked.

Run: python -m unittest discover -s tests -v
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bot'))
import youtube  # noqa: E402


def make_folder(root: Path, name: str, uploaded_days_ago=None, status='processed') -> Path:
    folder = root / name
    folder.mkdir()
    for f in (f'{name}.mp4', f'{name}.txt', f'{name}.srt', '.metadata.json'):
        (folder / f).write_text('x')
    if uploaded_days_ago is not None:
        when = datetime.now(timezone.utc) - timedelta(days=uploaded_days_ago)
        youtube._write_marker(folder, {
            'video_id': 'vid123', 'url': 'https://youtu.be/vid123', 'privacy': 'unlisted',
            'uploaded_at': when.isoformat(), 'status': status,
        })
    return folder


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_cleanup(self, state=('processed', ''), **kw):
        with patch.object(youtube, 'check_status', return_value=state) as check:
            deleted = youtube.cleanup_expired(self.root, **kw)
        return deleted, check

    def test_expired_folder_is_deleted_with_everything_in_it(self):
        folder = make_folder(self.root, 'old', uploaded_days_ago=8)
        deleted, check = self.run_cleanup()
        self.assertEqual(deleted, [folder])
        self.assertFalse(folder.exists())
        check.assert_called_once_with('vid123')

    def test_folder_inside_retention_window_is_kept(self):
        folder = make_folder(self.root, 'fresh', uploaded_days_ago=6)
        deleted, check = self.run_cleanup()
        self.assertEqual(deleted, [])
        self.assertTrue((folder / 'fresh.mp4').exists())
        check.assert_not_called()

    def test_folder_without_marker_is_never_deleted(self):
        folder = make_folder(self.root, 'failed-upload')
        old = datetime.now(timezone.utc) - timedelta(days=365)
        deleted, check = self.run_cleanup(now=old + timedelta(days=3650))
        self.assertEqual(deleted, [])
        self.assertTrue(folder.exists())
        check.assert_not_called()

    def test_kept_when_youtube_no_longer_reports_processed(self):
        for state in (('processing', ''), ('missing', 'gone'), ('failed', 'rejected')):
            with self.subTest(state=state):
                folder = make_folder(self.root, f'v-{state[0]}', uploaded_days_ago=30)
                deleted, _ = self.run_cleanup(state=state)
                self.assertEqual(deleted, [])
                self.assertTrue(folder.exists())

    def test_kept_when_youtube_cannot_be_reached(self):
        folder = make_folder(self.root, 'offline', uploaded_days_ago=30)
        with patch.object(youtube, 'check_status', side_effect=OSError('network down')):
            deleted = youtube.cleanup_expired(self.root)
        self.assertEqual(deleted, [])
        self.assertTrue(folder.exists())

    def test_active_folder_is_kept(self):
        folder = make_folder(self.root, 'live', uploaded_days_ago=30)
        deleted, _ = self.run_cleanup(is_active_folder=lambda p: p == folder)
        self.assertEqual(deleted, [])
        self.assertTrue(folder.exists())

    def test_corrupt_marker_is_ignored(self):
        folder = make_folder(self.root, 'corrupt')
        youtube.marker_path(folder).write_text('{not json')
        deleted, _ = self.run_cleanup()
        self.assertEqual(deleted, [])
        self.assertTrue(folder.exists())

    def test_only_expired_folders_among_several_are_deleted(self):
        old = make_folder(self.root, 'a-old', uploaded_days_ago=10)
        new = make_folder(self.root, 'b-new', uploaded_days_ago=1)
        none = make_folder(self.root, 'c-none')
        deleted, _ = self.run_cleanup()
        self.assertEqual(deleted, [old])
        self.assertTrue(new.exists() and none.exists())


class UploadRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / 'Meeting_20261028'
        self.folder.mkdir()
        self.mp4 = self.folder / 'Meeting_20261028.mp4'
        self.mp4.write_text('video')

    async def test_success_writes_processed_marker(self):
        with patch.object(youtube, 'upload_file', return_value=('vid9', 'unlisted')), \
             patch.object(youtube, 'check_status', return_value=('processed', '')):
            marker = await youtube.upload_recording(self.mp4)
        self.assertEqual(marker['status'], 'processed')
        self.assertEqual(marker['url'], 'https://youtu.be/vid9')
        self.assertEqual(youtube.read_marker(self.folder)['video_id'], 'vid9')
        self.assertGreater(youtube.expiry_of(marker), datetime.now(timezone.utc) + timedelta(days=6))

    async def test_failed_upload_leaves_no_marker_and_keeps_files(self):
        stale = {'video_id': 'old', 'uploaded_at': datetime.now(timezone.utc).isoformat()}
        youtube._write_marker(self.folder, stale)
        with patch.object(youtube, 'upload_file', side_effect=RuntimeError('quota')):
            with self.assertRaises(RuntimeError):
                await youtube.upload_recording(self.mp4)
        self.assertIsNone(youtube.read_marker(self.folder))
        self.assertTrue(self.mp4.exists())

    async def test_rejected_video_removes_marker(self):
        with patch.object(youtube, 'upload_file', return_value=('vid9', 'private')), \
             patch.object(youtube, 'check_status', return_value=('failed', 'uploadLimitExceeded')):
            with self.assertRaisesRegex(youtube.YouTubeError, 'uploadLimitExceeded'):
                await youtube.upload_recording(self.mp4)
        self.assertIsNone(youtube.read_marker(self.folder))
        self.assertTrue(self.mp4.exists())

    async def test_still_processing_after_timeout_keeps_processing_marker(self):
        with patch.object(youtube, 'upload_file', return_value=('vid9', 'unlisted')), \
             patch.object(youtube, 'check_status', return_value=('processing', '')), \
             patch.object(youtube, 'POLL_TIMEOUT_SECS', 0):
            marker = await youtube.upload_recording(self.mp4)
        self.assertEqual(marker['status'], 'processing')
        self.assertEqual(youtube.read_marker(self.folder)['status'], 'processing')


if __name__ == '__main__':
    unittest.main()
