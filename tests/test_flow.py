import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, MessageId

from kinogid import content
from kinogid.config import Config
from kinogid.db import Database
from kinogid.handlers import TrackingMiddleware, build_router
from kinogid.worker import deliver_lesson_followup

ADMIN = 759470806


class OfflineSession(BaseSession):
    """Пропускает настоящую маршрутизацию aiogram, не обращаясь к Telegram."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.sequence = 1000

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        self.sequence += 1
        name = method.__api_method__
        if name == "copyMessage":
            return MessageId(message_id=self.sequence)
        if name in {"sendMessage", "sendDocument", "editMessageReplyMarkup"}:
            return Message.model_validate({
                "message_id": self.sequence, "date": 1780000000,
                "chat": {"id": int(method.chat_id), "type": "private"},
                "text": getattr(method, "text", None),
            }, context={"bot": bot})
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "bot.sqlite3"
        self.config = Config("123456789:offline-test-token", frozenset({ADMIN}), path, ZoneInfo("Europe/Moscow"))
        self.db = Database(path, self.config.admin_ids)
        self.session = OfflineSession()
        self.bot = Bot(self.config.token, session=self.session)
        self.dispatcher = Dispatcher(db=self.db, config=self.config)
        self.dispatcher.message.outer_middleware(TrackingMiddleware())
        self.dispatcher.callback_query.outer_middleware(TrackingMiddleware())
        self.dispatcher.include_router(build_router(self.config))
        self.update_id = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.db.close()
        self.temp.cleanup()

    def user(self, user_id):
        return {"id": user_id, "is_bot": False, "first_name": "<Даша>", "username": "viewer"}

    async def message(self, text, user_id=100, chat_type="private", **extra):
        self.update_id += 1
        payload = {"message_id": self.update_id, "date": 1780000000,
                   "from": self.user(user_id), "chat": {"id": user_id, "type": chat_type},
                   "text": text, **extra}
        await self.dispatcher.feed_raw_update(self.bot, {"update_id": self.update_id, "message": payload})

    async def click(self, data, user_id=100):
        self.update_id += 1
        await self.dispatcher.feed_raw_update(self.bot, {
            "update_id": self.update_id,
            "callback_query": {"id": str(self.update_id), "from": self.user(user_id),
                               "chat_instance": "offline", "data": data,
                               "message": {"message_id": 500, "date": 1780000000,
                                           "chat": {"id": user_id, "type": "private"}}},
        })

    def texts(self):
        return [call.text for call in self.session.calls if call.__api_method__ == "sendMessage"]

    async def test_lesson_poll_and_guide_follow_the_requested_schedule(self):
        start = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        with patch("kinogid.db.utcnow", return_value=start) as clock:
            await self.message("/start")
            welcome = self.session.calls[-1].reply_markup.inline_keyboard
            self.assertEqual(welcome[0][0].url, content.CLUB_URL)
            self.assertEqual(welcome[1][0].callback_data, "lesson:show")

            await self.click("lesson:show")
            self.assertEqual(self.session.calls[-2].__api_method__, "sendPhoto")
            self.assertEqual(self.texts()[-1], content.LESSON)
            self.assertEqual(self.session.calls[-1].reply_markup.inline_keyboard[0][0].url,
                             content.LESSON_URL)

            clock.return_value = start + timedelta(minutes=29)
            await self.click("lesson:show")
            self.assertIsNone(self.db.next_lesson_followup())
            clock.return_value += timedelta(minutes=1)
            poll = self.db.next_lesson_followup()
            await deliver_lesson_followup(self.bot, self.db, poll)
            self.assertEqual(self.texts()[-1], "кто ты в кино?")
            self.assertEqual(len(self.session.calls[-1].reply_markup.inline_keyboard), 5)
            self.assertIsNone(self.db.next_lesson_followup())

            clock.return_value += timedelta(minutes=1)
            await deliver_lesson_followup(self.bot, self.db, self.db.next_lesson_followup())
            self.assertEqual(self.texts()[-1], content.GUIDE[0])
            self.assertIsNone(self.db.next_lesson_followup())

            await self.click("role:editor")
            self.assertEqual(self.db.user(100)["role"], "editor")
            self.assertEqual(self.texts()[-1], content.ACK)

    async def test_full_onboarding_role_and_training(self):
        await self.message("/start instagram")
        self.assertIn(content.WELCOME, self.texts())
        self.assertIsNone(self.db.user(100)["joined_at"])
        await self.click("club:join")
        self.assertIsNotNone(self.db.user(100)["joined_at"])
        self.assertIn(content.GUIDE[0], self.texts())
        self.assertEqual(self.texts()[-1], "кто ты в кино?")
        await self.click("role:editor")
        await self.click("training:on")
        self.assertEqual(self.db.user(100)["role"], "editor")
        self.assertIsNotNone(self.db.user(100)["training_interest_at"])
        self.assertEqual(self.texts()[-1], content.ACK)
        self.assertEqual(self.db.user(100)["source"], "instagram")

    async def test_repeated_join_does_not_repeat_guide(self):
        await self.message("/start")
        await self.click("club:join")
        await self.click("club:join")
        self.assertEqual(self.texts().count(content.GUIDE[0]), 1)
        self.assertEqual(self.db.stats()["joined"], 1)

    async def test_non_admin_cannot_export_view_stats_or_confirm_job(self):
        await self.message("/start", ADMIN)
        await self.message("/post all", ADMIN, reply_to_message={
            "message_id": 3, "date": 1780000000,
            "chat": {"id": ADMIN, "type": "private"}, "text": "Фильм недели",
        })
        self.assertEqual(self.db.job(1)["state"], "draft")
        await self.click("job:confirm:1")
        self.assertEqual(self.db.job(1)["state"], "draft")
        await self.message("/stats")
        await self.message("/export")
        await self.message("/user 100")
        self.assertFalse(any(c.__api_method__ == "sendDocument" for c in self.session.calls))
        self.assertFalse(any("статистика</b>" in text for text in self.texts()))

    async def test_admin_can_preview_confirm_and_access_stats_but_is_not_saved(self):
        await self.message("/start")
        await self.click("club:join")
        await self.message("/start", ADMIN)
        await self.click("club:join", ADMIN)
        await self.message("/stats", ADMIN)
        self.assertIsNone(self.db.user(ADMIN))
        self.assertIn("Всего пользователей: <b>1</b>", self.texts()[-1])
        await self.message("/post club", ADMIN, reply_to_message={
            "message_id": 3, "date": 1780000000,
            "chat": {"id": ADMIN, "type": "private"}, "text": "Фильм недели",
        })
        await self.click("job:confirm:1", ADMIN)
        self.assertEqual(self.db.job(1)["state"], "pending")
        self.assertEqual(self.db.job_counts(1)["pending"], 1)
        await self.click("job:confirm:1", ADMIN)
        self.assertEqual(self.db.job_counts(1)["pending"], 1)

    async def test_stop_survives_start_and_join_reenables(self):
        await self.message("/start")
        await self.click("club:join")
        await self.message("/stop")
        await self.message("/start")
        self.assertFalse(self.db.eligible(100, "club"))
        await self.message("/club")
        self.assertTrue(self.db.eligible(100, "club"))

    async def test_group_messages_are_ignored(self):
        await self.message("/start", chat_type="group")
        self.assertEqual(self.db.stats()["total"], 0)
        self.assertEqual(len(self.session.calls), 0)

    async def test_export_and_user_card_escape_user_controlled_names(self):
        await self.message("/start")
        await self.message("/user 100", ADMIN)
        self.assertIn("&lt;Даша&gt;", self.texts()[-1])
        await self.message("/export", ADMIN)
        self.assertTrue(any(c.__api_method__ == "sendDocument" for c in self.session.calls))
