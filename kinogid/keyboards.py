from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .content import CLUB_URL, LESSON_URL, ROLES


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вступить в киноклуб", url=CLUB_URL)],
        [InlineKeyboardButton(text="Получить урок", callback_data="lesson:show")],
    ])


def lesson_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ЗАБРАТЬ УРОК", url=LESSON_URL)],
    ])


def profile_keyboard(role: str | None = None, interested: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=label + (" ✓" if key == role else ""), callback_data=f"role:{key}"
    )] for key, label in ROLES.items()]
    rows.append([InlineKeyboardButton(
        text="хочу попасть в Vision Мастерскую⭐️" + (" ✓" if interested else ""),
        callback_data="training:off" if interested else "training:on",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_keyboard(club_url: str = "") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🍿 Как смотреть кино", callback_data="guide:show")],
        [InlineKeyboardButton(text="Кто я в кино / обучение", callback_data="profile:show")],
    ]
    if club_url:
        rows.append([InlineKeyboardButton(text="💬 Чат киноклуба", url=club_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить рассылку", callback_data=f"job:confirm:{job_id}")],
        [InlineKeyboardButton(text="Отменить", callback_data=f"job:cancel:{job_id}")],
    ])
