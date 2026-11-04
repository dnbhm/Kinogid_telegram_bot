import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessage

from kinogid.db import Database, utcnow
from kinogid.worker import deliver_job

ADMIN = 759470806


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3", frozenset({ADMIN}))
        self.db.touch(SimpleNamespace(id=100, is_bot=False, username=None,
                                     first_name="Даша", last_name=None, language_code="ru"))
        self.db.join(100)
        self.job_id = self.db.create_job(ADMIN, ADMIN, 1, "club", utcnow())
        self.db.confirm_job(self.job_id)
        self.job = self.db.next_job()
        self.bot = SimpleNamespace(copy_message=AsyncMock())
        self.method = CopyMessage(chat_id=100, from_chat_id=ADMIN, message_id=1)
        self.sleep_patch = patch("kinogid.worker.asyncio.sleep", new=AsyncMock())
        self.sleep_patch.start()

    async def asyncTearDown(self):
        self.sleep_patch.stop()
        self.db.close()
        self.temp.cleanup()

    async def test_success_is_saved_and_not_resent_after_restart(self):
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.job_counts(self.job_id)["sent"], 1)
        self.assertEqual(self.db.job(self.job_id)["state"], "done")
        self.assertEqual(self.db.user(100)["posts_received"], 1)
        await deliver_job(self.bot, self.db, self.job)
        self.bot.copy_message.assert_awaited_once()

    async def test_stopped_user_is_skipped_even_after_confirmation(self):
        self.db.set_notifications(100, False)
        await deliver_job(self.bot, self.db, self.job)
        self.bot.copy_message.assert_not_called()
        self.assertEqual(self.db.job_counts(self.job_id)["skipped"], 1)

    async def test_blocked_user_is_saved_without_crashing_campaign(self):
        self.bot.copy_message.side_effect = TelegramForbiddenError(method=self.method, message="blocked")
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.user(100)["blocked"], 1)
        self.assertEqual(self.db.job_counts(self.job_id)["skipped"], 1)
        self.assertEqual(self.db.job(self.job_id)["state"], "done")

    async def test_telegram_limit_retries_without_losing_recipient(self):
        self.bot.copy_message.side_effect = [
            TelegramRetryAfter(method=self.method, message="retry", retry_after=1), None
        ]
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.bot.copy_message.await_count, 2)
        self.assertEqual(self.db.job_counts(self.job_id)["sent"], 1)

    async def test_network_failures_are_bounded_and_visible(self):
        self.bot.copy_message.side_effect = TelegramNetworkError(method=self.method, message="offline")
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.bot.copy_message.await_count, 5)
        self.assertEqual(self.db.job_counts(self.job_id)["failed"], 1)

    async def test_missing_source_stops_job(self):
        self.bot.copy_message.side_effect = TelegramBadRequest(method=self.method, message="Bad Request: message to copy not found")
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.job(self.job_id)["state"], "failed")

    async def test_cancelled_job_does_not_send_next_message(self):
        self.db.cancel_job(self.job_id)
        await deliver_job(self.bot, self.db, self.job)
        self.bot.copy_message.assert_not_called()
