import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendPhoto
from aiogram.types import FSInputFile

from kinogid import __main__ as app, intensive
from kinogid.config import Config
from kinogid.db import Database
from kinogid.worker import deliver_job
import test_flow

ADMIN = test_flow.ADMIN
BEFORE = intensive.SEND_AT - timedelta(days=1)


def person(user_id):
    return SimpleNamespace(id=user_id, is_bot=False, username=None, first_name="Участник",
                           last_name=None, language_code="ru")


class AutomaticDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path, frozenset({ADMIN}))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def register(self):
        return intensive.register_automatic(self.db, now=BEFORE)

    def test_pending_without_confirmation_and_not_sent_before_moscow_noon(self):
        job_id = self.register()
        job = self.db.job(job_id)
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["send_at"], "2026-11-22T09:00:00+00:00")
        self.assertEqual(job["content_kind"], "intensive")
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT - timedelta(seconds=1)):
            self.assertIsNone(self.db.next_job())
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            self.assertEqual(self.db.next_job()["id"], job_id)

    def test_all_stages_and_new_users_are_included_at_send_time(self):
        self.db.touch(person(100), start=True)
        self.db.touch(person(101))
        self.db.set_role(101, "editor")
        self.db.touch(person(102))
        self.db.join(102)
        self.db.set_interest(102, True)
        self.db.touch(person(103))
        self.db.set_notifications(103, False)
        self.db.touch(person(104))
        self.db.set_blocked(104, True)
        self.db.touch(person(ADMIN))
        job_id = self.register()
        self.assertEqual(self.db.job_counts(job_id)["pending"], 0)
        self.db.touch(person(105), start=True)
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            self.db.next_job()
        recipients = [row[0] for row in self.db.connection.execute(
            "SELECT user_id FROM deliveries ORDER BY user_id")]
        self.assertEqual(recipients, [100, 101, 102, 105])

    def test_restarts_keep_one_campaign_and_the_fixed_date(self):
        job_id = self.register()
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.assertEqual(self.register(), job_id)
        self.assertEqual(self.register(), job_id)
        self.assertEqual(len(self.db.recent_jobs()), 1)
        self.assertEqual(self.db.job(job_id)["send_at"], "2026-11-22T09:00:00+00:00")

    def test_restart_does_not_expand_an_already_started_audience(self):
        self.db.touch(person(100))
        job_id = self.register()
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            self.db.next_job()
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.db.touch(person(101))
        intensive.register_automatic(self.db, now=intensive.SEND_AT + timedelta(minutes=1))
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT + timedelta(minutes=1)):
            self.db.next_job()
        self.assertEqual(self.db.job_counts(job_id)["pending"], 1)

    def test_explicit_cancellation_survives_restart_registration(self):
        job_id = self.register()
        self.db.cancel_job(job_id)
        self.assertEqual(self.register(), job_id)
        self.assertEqual(self.db.job(job_id)["state"], "cancelled")

    def test_completed_campaign_is_not_created_again(self):
        job_id = self.register()
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            self.db.next_job()
        self.db.finish_job(job_id)
        self.assertEqual(self.register(), job_id)
        self.assertEqual(self.db.job(job_id)["state"], "done")
        self.assertEqual(len(self.db.recent_jobs()), 1)

    def test_previous_november_21_campaign_is_cancelled(self):
        old_id = self.db.create_job(
            ADMIN, ADMIN, 11, "all", BEFORE, campaign_key=intensive.SUPERSEDED_KEYS[0])
        self.db.confirm_job(old_id)
        new_id = self.register()
        self.assertEqual(self.db.job(old_id)["state"], "cancelled")
        self.assertEqual(self.db.job(new_id)["state"], "pending")

    def test_first_install_after_deadline_does_not_send_stale_post(self):
        self.assertIsNone(intensive.register_automatic(self.db, now=intensive.SEND_AT))
        self.assertEqual(len(self.db.recent_jobs()), 0)

    def test_migration_preserves_users_lesson_schedule_and_existing_jobs(self):
        self.db.touch(person(100))
        self.db.schedule_lesson_poll(100)
        job_id = self.db.create_job(ADMIN, ADMIN, 11, "all", BEFORE)
        self.db.confirm_job(job_id)
        self.db.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP INDEX idx_jobs_campaign")
        for column in ("source_message_ids", "reply_markup_json", "audience_at_send", "audience_ready",
                       "campaign_key", "content_kind", "media_file_id"):
            connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        connection.execute("ALTER TABLE deliveries DROP COLUMN next_part")
        connection.commit()
        connection.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.assertIsNotNone(self.db.user(100))
        self.assertEqual(self.db.job_counts(job_id)["pending"], 1)
        self.assertEqual(self.db.job(job_id)["content_kind"], "copy")
        self.assertEqual(self.db.delivery_part(job_id, 100), 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM lesson_followups").fetchone()[0], 1)


class AutomaticWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path, frozenset({ADMIN}))
        self.db.touch(person(100), start=True)
        self.job_id = intensive.register_automatic(self.db, now=BEFORE)
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            self.job = self.db.next_job()
        self.photo = SimpleNamespace(photo=[SimpleNamespace(file_id="cached-original-photo")])
        self.bot = SimpleNamespace(send_photo=AsyncMock(return_value=self.photo),
                                   send_message=AsyncMock(), copy_message=AsyncMock())
        self.sleep = patch("kinogid.worker.asyncio.sleep", new=AsyncMock())
        self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        self.db.close()
        self.temp.cleanup()

    async def test_original_photo_then_full_text_with_correct_url_button(self):
        await deliver_job(self.bot, self.db, self.job)
        self.bot.send_photo.assert_awaited_once()
        photo = self.bot.send_photo.await_args.kwargs["photo"]
        self.assertIsInstance(photo, FSInputFile)
        self.assertEqual(photo.path, intensive.PHOTO)
        self.bot.copy_message.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()
        message = self.bot.send_message.await_args.kwargs
        self.assertEqual(message["chat_id"], 100)
        self.assertEqual(message["text"], intensive.TEXT)
        self.assertEqual(message["parse_mode"], "HTML")
        button = message["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "Вступить в киноклуб🎥")
        self.assertEqual(button.url, "https://t.me/+xb3ZBglvse5kMGEy")
        self.assertEqual(self.db.user(100)["posts_received"], 1)
        self.assertEqual(self.db.job(self.job_id)["state"], "done")

    async def test_photo_is_reused_for_remaining_recipients(self):
        self.db.touch(person(101))
        with self.db.connection:
            self.db.connection.execute("INSERT INTO deliveries (job_id, user_id) VALUES (?, ?)",
                                       (self.job_id, 101))
        await deliver_job(self.bot, self.db, self.job)
        photos = self.bot.send_photo.await_args_list
        self.assertIsInstance(photos[0].kwargs["photo"], FSInputFile)
        self.assertEqual(photos[1].kwargs["photo"], "cached-original-photo")
        self.assertEqual(self.db.job_counts(self.job_id)["sent"], 2)

    async def test_restart_after_photo_resumes_from_text(self):
        self.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.delivery_part(self.job_id, 100), 1)
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        intensive.register_automatic(self.db, now=intensive.SEND_AT)
        with patch("kinogid.db.utcnow", return_value=intensive.SEND_AT):
            job = self.db.next_job()
        self.bot.send_photo.reset_mock()
        self.bot.send_message = AsyncMock()
        await deliver_job(self.bot, self.db, job)
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.db.job_counts(self.job_id)["sent"], 1)

    async def test_unsubscribe_between_photo_and_text_is_respected(self):
        async def send_photo(**kwargs):
            self.db.set_notifications(100, False)
            return self.photo
        self.bot.send_photo.side_effect = send_photo
        await deliver_job(self.bot, self.db, self.job)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.job_counts(self.job_id)["skipped"], 1)

    async def test_blocked_user_does_not_interrupt_campaign(self):
        self.bot.send_photo.side_effect = TelegramForbiddenError(
            method=SendPhoto(chat_id=100, photo="photo"), message="bot was blocked by the user")
        await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.user(100)["blocked"], 1)
        self.assertEqual(self.db.job_counts(self.job_id)["skipped"], 1)
        self.assertEqual(self.db.job(self.job_id)["state"], "done")


class AutomaticStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_registers_ready_campaign_without_any_admin_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            config = Config("123456789:offline-test-token", frozenset({ADMIN}), path,
                            ZoneInfo("Europe/Moscow"))
            bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="offline")),
                                  delete_webhook=AsyncMock(), session=SimpleNamespace(close=AsyncMock()),
                                  send_photo=AsyncMock(), send_message=AsyncMock())
            dispatcher = MagicMock()
            dispatcher.start_polling = AsyncMock()
            dispatcher.resolve_used_update_types.return_value = ["message"]
            with patch.object(app.Config, "from_env", return_value=config), \
                 patch.object(app, "Bot", return_value=bot), \
                 patch.object(app, "Dispatcher", return_value=dispatcher), \
                 patch.object(app, "setup_profile", new=AsyncMock()), \
                 patch("kinogid.db.utcnow", return_value=BEFORE):
                await app.main()
            db = Database(path, config.admin_ids)
            try:
                job = db.campaign_job(intensive.CAMPAIGN_KEY)
                self.assertEqual(job["state"], "pending")
                self.assertEqual(job["send_at"], "2026-11-22T09:00:00+00:00")
                self.assertEqual(db.stats()["total"], 0)
            finally:
                db.close()
            bot.send_photo.assert_not_awaited()
            bot.send_message.assert_not_awaited()


class AutomaticPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_flow.FlowTests.asyncSetUp
    asyncTearDown = test_flow.FlowTests.asyncTearDown
    user = test_flow.FlowTests.user
    message = test_flow.FlowTests.message
    click = test_flow.FlowTests.click
    texts = test_flow.FlowTests.texts

    async def test_optional_preview_has_no_confirmation_and_no_duplicate_job(self):
        with patch("kinogid.db.utcnow", return_value=BEFORE):
            await self.message("/intensive", ADMIN)
            await self.message("/intensive", ADMIN)
        self.assertEqual(len(self.db.recent_jobs()), 1)
        self.assertEqual(self.db.campaign_job(intensive.CAMPAIGN_KEY)["state"], "pending")
        self.assertIn("Подтверждать ничего не нужно", self.texts()[-1])
        for call in self.session.calls:
            markup = getattr(call, "reply_markup", None)
            if markup:
                for row in markup.inline_keyboard:
                    for button in row:
                        self.assertFalse((button.callback_data or "").startswith("job:confirm:"))

    async def test_non_admin_cannot_create_or_preview_campaign(self):
        await self.message("/intensive", 100)
        self.assertIsNone(self.db.campaign_job(intensive.CAMPAIGN_KEY))
        self.assertFalse(any(call.__api_method__ == "sendPhoto" for call in self.session.calls))


class AutomaticContentTests(unittest.TestCase):
    def test_photo_is_the_exact_original_asset(self):
        self.assertEqual(hashlib.sha256(intensive.PHOTO.read_bytes()).hexdigest(),
                         "43919d0b36c85a9ba4d2ea1062fdef2f5443b05dc7af2288cba66dbf38d710e7")

    def test_full_text_is_valid_and_date_is_fixed_to_moscow_noon(self):
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = ""
                self.stack = []

            def handle_starttag(self, tag, attrs):
                self.stack.append(tag)

            def handle_endtag(self, tag):
                assert self.stack.pop() == tag

            def handle_data(self, data):
                self.text += data

        parser = Parser()
        parser.feed(intensive.TEXT)
        self.assertEqual(parser.stack, [])
        self.assertLessEqual(len(parser.text.encode("utf-16-le")) // 2, 4096)
        self.assertIn("с 21 по 23 ноября", parser.text)
        self.assertEqual(intensive.SEND_AT.isoformat(), "2026-11-22T12:00:00+03:00")
