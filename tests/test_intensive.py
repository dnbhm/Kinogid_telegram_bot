import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from kinogid import intensive
from kinogid.db import Database, utcnow
from kinogid.worker import deliver_job
import test_flow

ADMIN = test_flow.ADMIN


def person(user_id):
    return SimpleNamespace(id=user_id, is_bot=False, username=None, first_name="Участник",
                           last_name=None, language_code="ru")


class IntensiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path, frozenset({ADMIN}))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def create(self):
        return self.db.create_job(
            ADMIN, ADMIN, 11, "all", utcnow(), message_ids=[11, 12],
            reply_markup_json=intensive.keyboard().model_dump_json(), audience_at_send=True,
            campaign_key=intensive.CAMPAIGN_KEY,
        )

    def test_users_are_selected_when_sending_including_people_who_only_pressed_start(self):
        self.db.touch(person(100), start=True)
        job_id = self.create()
        self.db.confirm_job(job_id)
        self.assertEqual(self.db.job_counts(job_id)["pending"], 0)
        self.db.touch(person(101), start=True)
        self.db.touch(person(102))
        self.db.set_notifications(102, False)
        self.db.touch(person(103))
        self.db.set_blocked(103, True)
        self.db.touch(person(ADMIN))
        self.db.next_job()
        recipients = [row[0] for row in self.db.connection.execute("SELECT user_id FROM deliveries")]
        self.assertEqual(recipients, [100, 101])

    def test_restarting_does_not_expand_already_started_audience(self):
        self.db.touch(person(100))
        job_id = self.create()
        self.db.confirm_job(job_id)
        self.db.next_job()
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.db.touch(person(101))
        self.db.next_job()
        self.assertEqual(self.db.job_counts(job_id)["pending"], 1)

    def test_duplicate_campaign_cannot_be_created(self):
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()

    def test_old_database_is_migrated_without_losing_profiles_or_jobs(self):
        self.db.touch(person(100))
        job_id = self.db.create_job(ADMIN, ADMIN, 11, "all", utcnow())
        self.db.confirm_job(job_id)
        self.db.close()
        # Воспроизводим старую схему до появления новых столбцов.
        connection = sqlite3.connect(self.path)
        connection.execute("DROP INDEX idx_jobs_campaign")
        for column in ("source_message_ids", "reply_markup_json", "audience_at_send", "audience_ready", "campaign_key"):
            connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        connection.execute("ALTER TABLE deliveries DROP COLUMN next_part")
        connection.commit()
        connection.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        self.assertIsNotNone(self.db.user(100))
        self.assertEqual(self.db.job_counts(job_id)["pending"], 1)
        self.assertEqual(self.db.delivery_part(job_id, 100), 0)
        self.assertIsNotNone(self.db.next_job())


class IntensiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path, frozenset({ADMIN}))
        self.db.touch(person(100), start=True)
        self.job_id = self.db.create_job(
            ADMIN, ADMIN, 11, "all", utcnow(), message_ids=[11, 12],
            reply_markup_json=intensive.keyboard().model_dump_json(), audience_at_send=True,
        )
        self.db.confirm_job(self.job_id)
        self.job = self.db.next_job()
        self.bot = SimpleNamespace(copy_message=AsyncMock())
        self.sleep = patch("kinogid.worker.asyncio.sleep", new=AsyncMock())
        self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        self.db.close()
        self.temp.cleanup()

    async def test_photo_then_full_text_with_correct_url_button(self):
        await deliver_job(self.bot, self.db, self.job)
        calls = self.bot.copy_message.await_args_list
        self.assertEqual([call.kwargs["message_id"] for call in calls], [11, 12])
        self.assertNotIn("reply_markup", calls[0].kwargs)
        button = calls[1].kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "Вступить в киноклуб🎥")
        self.assertEqual(button.url, "https://t.me/+xb3ZBglvse5kMGEy")
        self.assertEqual(self.db.user(100)["posts_received"], 1)

    async def test_restart_after_photo_resumes_from_text_without_repeating_photo(self):
        self.bot.copy_message.side_effect = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await deliver_job(self.bot, self.db, self.job)
        self.assertEqual(self.db.delivery_part(self.job_id, 100), 1)
        self.db.close()
        self.db = Database(self.path, frozenset({ADMIN}))
        job = self.db.next_job()
        self.bot.copy_message = AsyncMock()
        await deliver_job(self.bot, self.db, job)
        self.bot.copy_message.assert_awaited_once()
        self.assertEqual(self.bot.copy_message.await_args.kwargs["message_id"], 12)
        self.assertEqual(self.db.job_counts(self.job_id)["sent"], 1)


class IntensiveFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_flow.FlowTests.asyncSetUp
    asyncTearDown = test_flow.FlowTests.asyncTearDown
    user = test_flow.FlowTests.user
    message = test_flow.FlowTests.message
    click = test_flow.FlowTests.click
    texts = test_flow.FlowTests.texts

    async def test_original_post_is_copied_with_button_and_without_replacing_caption(self):
        with patch.object(intensive, "SEND_AT", utcnow() + timedelta(days=1)):
            await self.message("/intensive", ADMIN, reply_to_message={
                "message_id": 20, "date": 1780000000,
                "chat": {"id": ADMIN, "type": "private"},
                "photo": [{"file_id": "original", "file_unique_id": "unique", "width": 1016, "height": 1280}],
                "caption": "Длинная подпись заказчика " * 60,
            })
        copies = [call for call in self.session.calls if call.__api_method__ == "copyMessage"]
        self.assertEqual(len(copies), 1)
        self.assertIsNone(copies[0].caption)
        self.assertEqual(copies[0].reply_markup.inline_keyboard[0][0].url, intensive.CLUB_URL)
        job = self.db.campaign_job(intensive.CAMPAIGN_KEY)
        self.assertEqual(job["audience"], "all")
        self.assertTrue(job["audience_at_send"])
        self.assertEqual(job["state"], "draft")
        await self.click(f"job:confirm:{job['id']}", ADMIN)
        self.assertEqual(self.db.job(job["id"])["state"], "pending")

    async def test_fallback_and_repeated_command_produce_one_draft(self):
        with patch.object(intensive, "SEND_AT", utcnow() + timedelta(days=1)):
            await self.message("/intensive", ADMIN)
            await self.message("/intensive", ADMIN)
        self.assertEqual(len(self.db.recent_jobs()), 1)
        self.assertTrue(any(call.__api_method__ == "sendPhoto" for call in self.session.calls))
        self.assertIn(intensive.TEXT, self.texts())

    async def test_non_admin_cannot_prepare_campaign(self):
        await self.message("/intensive", 100)
        self.assertIsNone(self.db.campaign_job(intensive.CAMPAIGN_KEY))

    async def test_past_time_does_not_turn_into_immediate_broadcast(self):
        with patch.object(intensive, "SEND_AT", utcnow() - timedelta(minutes=1)):
            await self.message("/intensive", ADMIN)
        self.assertIsNone(self.db.campaign_job(intensive.CAMPAIGN_KEY))
        self.assertIn("уже прошло", self.texts()[-1])

    async def test_late_confirmation_does_not_schedule_a_past_campaign(self):
        with patch.object(intensive, "SEND_AT", utcnow() + timedelta(days=1)):
            await self.message("/intensive", ADMIN)
        job = self.db.campaign_job(intensive.CAMPAIGN_KEY)
        with patch.object(intensive, "SEND_AT", utcnow() - timedelta(minutes=1)):
            await self.click(f"job:confirm:{job['id']}", ADMIN)
        self.assertEqual(self.db.job(job["id"])["state"], "draft")


class IntensiveContentTests(unittest.TestCase):
    def test_text_and_photo_are_ready_and_timing_is_moscow_noon(self):
        class Parser(HTMLParser):
            text = ""
            stack = []

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
        self.assertTrue(intensive.PHOTO.is_file())
        self.assertEqual(intensive.SEND_AT.isoformat(), "2026-11-21T12:00:00+03:00")
