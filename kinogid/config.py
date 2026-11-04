import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    db_path: Path
    timezone: ZoneInfo
    club_chat_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token == "PASTE_BOTFATHER_TOKEN_HERE":
            raise ValueError("Добавьте настоящий BOT_TOKEN в .env")
        try:
            admin_ids = frozenset(
                int(value.strip())
                for value in os.getenv("ADMIN_IDS", "759470806").split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError("ADMIN_IDS: нужны числовые Telegram ID через запятую") from exc
        if not admin_ids or any(value <= 0 for value in admin_ids):
            raise ValueError("Укажите хотя бы один положительный ADMIN_IDS")
        try:
            timezone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Неизвестный TIMEZONE. Пример: Europe/Moscow") from exc
        club_url = os.getenv("CLUB_CHAT_URL", "").strip()
        if club_url:
            parsed = urlparse(club_url)
            if parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}:
                raise ValueError("CLUB_CHAT_URL: нужна ссылка https://t.me/...")
        return cls(
            token=token,
            admin_ids=admin_ids,
            db_path=Path(os.getenv("DB_PATH", "data/kinogid.sqlite3")),
            timezone=timezone,
            club_chat_url=club_url,
        )
