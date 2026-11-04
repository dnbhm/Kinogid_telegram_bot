import csv
import io
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from kinogid import content
from kinogid.keyboards import profile_keyboard
from kinogid.utils import csv_bytes, parse_send_at


class TelegramHTMLValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.plain = ""

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "a", "blockquote"}, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag, tag

    def handle_data(self, data):
        self.plain += data


class ContentTests(unittest.TestCase):
    def test_texts_have_balanced_html_and_fit_telegram_utf16_limits(self):
        self.assertLessEqual(len(content.DESCRIPTION.encode("utf-16-le")) // 2, 512)
        self.assertLessEqual(len(content.SHORT_DESCRIPTION.encode("utf-16-le")) // 2, 120)
        for text in (content.WELCOME, *content.GUIDE, content.ADMIN_HELP):
            parser = TelegramHTMLValidator()
            parser.feed(text)
            self.assertFalse(parser.stack)
            self.assertLessEqual(len(parser.plain.encode("utf-16-le")) // 2, 4096)

    def test_survey_has_four_roles_and_separate_training_choice(self):
        keyboard = profile_keyboard("editor", True)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 5)
        self.assertEqual(buttons[-1].callback_data, "training:off")
        for button in buttons:
            self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_csv_preserves_russian_and_neutralizes_spreadsheet_formulas(self):
        data = csv_bytes([{"user_id": 123, "first_name": "=1+1", "username": "@danger", "last_name": "Даша"}])
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0]["first_name"], "'=1+1")
        self.assertEqual(rows[0]["username"], "'@danger")
        self.assertEqual(rows[0]["last_name"], "Даша")

    def test_schedule_is_in_moscow_and_rejects_past_or_malformed_dates(self):
        timezone = ZoneInfo("Europe/Moscow")
        future = (datetime.now(timezone) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M").split()
        self.assertEqual(parse_send_at(future, timezone).utcoffset(), timedelta(hours=3))
        for args in (["2020-01-01", "12:00"], ["bad", "date"], ["tomorrow"]):
            with self.assertRaises(ValueError):
                parse_send_at(args, timezone)
