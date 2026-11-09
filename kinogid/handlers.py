import logging
import math
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    ErrorEvent,
    FSInputFile,
    Message,
)

from . import content
from .config import Config
from .db import AUDIENCES, Database, utcnow
from .keyboards import confirm_keyboard, lesson_keyboard, menu_keyboard, profile_keyboard, welcome_keyboard
from .utils import csv_bytes, local_time, parse_send_at

log = logging.getLogger(__name__)
LESSON_PHOTO = Path(__file__).with_name("lesson.png")


class TrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        db: Database = data["db"]
        if isinstance(event, Message) and event.chat.type == "private" and event.from_user:
            parts = (event.text or "").split(maxsplit=1)
            start = bool(parts and parts[0].split("@")[0].lower() == "/start")
            db.touch(event.from_user, start=start, source=parts[1] if start and len(parts) > 1 else "")
        elif isinstance(event, CallbackQuery) and event.message and event.message.chat.type == "private":
            db.touch(event.from_user, click=True)
        return await handler(event, data)


async def send_profile(bot: Bot, chat_id: int, db: Database, *, summary: bool = False) -> None:
    user = db.user(chat_id)
    text = "кто ты в кино?"
    if summary and user:
        role = content.ROLES.get(user["role"], "пока не выбрано")
        interest = "да" if user["training_interest_at"] else "пока нет"
        notifications = "включены" if user["notifications"] else "выключены — /club, чтобы вернуться"
        text += f"\n\nтвой ответ: {role}\nинтерес к обучению: {interest}\nсообщения: {notifications}"
    await bot.send_message(chat_id, text, reply_markup=profile_keyboard(
        user["role"] if user else None, bool(user and user["training_interest_at"])
    ))


async def show_guide(bot: Bot, chat_id: int) -> None:
    for part in content.GUIDE:
        await bot.send_message(chat_id, part)


async def join_club(bot: Bot, chat_id: int, db: Database, config: Config) -> None:
    db.join(chat_id)
    user = db.user(chat_id)
    step = user["onboarding_step"] if user else 0
    # После сбоя продолжаем с первого неотправленного сообщения.
    for index, part in enumerate(content.GUIDE):
        if index >= step:
            await bot.send_message(chat_id, part)
            db.set_onboarding_step(chat_id, index + 1)
    if step <= len(content.GUIDE):
        await send_profile(bot, chat_id, db)
        db.set_onboarding_step(chat_id, len(content.GUIDE) + 1)
    else:
        await bot.send_message(chat_id, "ты уже в зале ✦ сообщения киноклуба включены", reply_markup=menu_keyboard(config.club_chat_url))
    if config.club_chat_url and step <= len(content.GUIDE):
        await bot.send_message(chat_id, "проходи и в наш чат 🎬", reply_markup=menu_keyboard(config.club_chat_url))


def build_router(config: Config) -> Router:
    root = Router(name="kinogid")
    admin = Router(name="admin")
    public = Router(name="public")
    root.include_routers(admin, public)
    admin.message.filter(F.chat.type == "private", F.from_user.id.in_(config.admin_ids))
    admin.callback_query.filter(F.message.chat.type == "private", F.from_user.id.in_(config.admin_ids))
    public.message.filter(F.chat.type == "private")
    public.callback_query.filter(F.message.chat.type == "private")

    @admin.message(Command("admin"))
    async def admin_help(message: Message):
        await message.answer(content.ADMIN_HELP)

    @admin.message(Command("stats"))
    async def stats(message: Message, db: Database):
        values = db.stats()
        text = (
            "<b>Киногид · статистика</b>\n\n"
            f"Всего пользователей: <b>{values['total']}</b>\n"
            f"Новых за 24 часа: {values['new_today']}\n"
            f"Новых за 7 дней: {values['new_week']}\n"
            f"Взаимодействовали за 7 дней: {values['active_week']}\n"
            f"Нажимали /start (всего нажатий): {values['starts']}\n\n"
            f"Вступали в киноклуб: {values['joined']}\n"
            f"Получают сообщения киноклуба: {values['club_active']}\n"
            f"Отключили сообщения: {values['stopped']}\n"
            f"Telegram запретил отправку: {values['blocked']}\n"
            f"Хотят в Vision Мастерскую: {values['training']}\n"
            f"Доставлено постов рассылок: {values['posts_received']}\n\n"
            "<b>Кто в кино</b>\n"
        )
        text += "\n".join(f"{label}: {values['roles'].get(key, 0)}" for key, label in content.ROLES.items())
        text += f"\nБез ответа: {values['roles'].get('none', 0)}\n\nАдминистраторы исключены."
        await message.answer(text)

    @admin.message(Command("users"))
    async def users(message: Message, command: CommandObject, db: Database):
        try:
            page = int(command.args or "1")
            if page < 1:
                raise ValueError
        except ValueError:
            await message.answer("Пример: /users 1")
            return
        pages = max(1, math.ceil(db.stats()["total"] / 20))
        rows = db.users(page=page)
        lines = [f"<b>Пользователи · страница {page}/{pages}</b>"]
        for row in rows:
            name = escape(row["first_name"][:40])
            username = "@" + escape(row["username"]) if row["username"] else "без username"
            lines.append(f"<code>{row['user_id']}</code> · {name} · {username}")
        if not rows:
            lines.append("Здесь пока никого.")
        lines.append("\nКарточка: /user ID\nСледующая страница: /users " + str(page + 1))
        await message.answer("\n".join(lines))

    @admin.message(Command("user"))
    async def user_card(message: Message, command: CommandObject, db: Database):
        try:
            user_id = int(command.args or "")
        except ValueError:
            await message.answer("Пример: /user 123456789")
            return
        row = db.user(user_id)
        if not row:
            await message.answer("Пользователь не найден. Администраторов в базе нет.")
            return
        name = escape(" ".join(filter(None, (row["first_name"], row["last_name"]))))
        username = "@" + escape(row["username"]) if row["username"] else "—"
        role = content.ROLES.get(row["role"], "—")
        await message.answer(
            f"<b>{name}</b> · <code>{user_id}</code>\n{username}\n\n"
            f"Первый контакт: {local_time(row['first_seen'], config.timezone)}\n"
            f"Последний контакт: {local_time(row['last_seen'], config.timezone)}\n"
            f"Вступил(а): {local_time(row['joined_at'], config.timezone)}\n"
            f"Роль: {role}\n"
            f"Интерес к обучению: {local_time(row['training_interest_at'], config.timezone)}\n"
            f"Сообщения включены: {'да' if row['notifications'] else 'нет'}\n"
            f"Отправка запрещена Telegram: {'да' if row['blocked'] else 'нет'}\n"
            f"Запусков /start: {row['starts']}\n"
            f"Сообщений боту: {row['messages']} · нажатий: {row['clicks']}\n"
            f"Получено постов рассылок: {row['posts_received']}\n"
            f"Источник /start: {escape(row['source'] or '—')}\n"
            f"Время: {config.timezone.key}"
        )

    @admin.message(Command("export"))
    async def export_users(message: Message, db: Database):
        rows = db.export()
        if not rows:
            await message.answer("Пользователей пока нет. Администраторы не сохраняются.")
            return
        document = BufferedInputFile(csv_bytes(rows), filename="kinogid-users.csv")
        await message.answer_document(document, caption="Пользователи без администраторов. Даты в CSV — UTC.")

    @admin.message(Command("post"))
    async def create_post(message: Message, command: CommandObject, db: Database, bot: Bot):
        parts = (command.args or "").split()
        source = message.reply_to_message
        if not parts or parts[0] not in AUDIENCES or len(parts) not in {1, 3} or not source:
            await message.answer(
                "Ответьте на готовый пост командой /post club\n"
                "С датой: /post club 2026-11-09 12:00\n"
                "Аудитории: " + ", ".join(AUDIENCES)
            )
            return
        if source.media_group_id:
            await message.answer("Это альбом. Пришлите каждое фото или видео отдельным сообщением.")
            return
        if source.content_type not in {"text", "photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker"}:
            await message.answer("Поддерживаются текст, фото, видео, анимация, файл, аудио, голосовое, кружок и стикер.")
            return
        try:
            send_at = parse_send_at(parts[1:], config.timezone) if len(parts) == 3 else utcnow()
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer("Копия поста для проверки:")
        try:
            preview = await bot.copy_message(
                chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=source.message_id
            )
        except TelegramAPIError:
            await message.answer("Не удалось скопировать пост. Пришлите его боту заново.")
            return
        job_id = db.create_job(message.from_user.id, message.chat.id, preview.message_id, parts[0], send_at)
        count = db.audience_count(parts[0])
        await message.answer(
            f"<b>Рассылка #{job_id}</b>\n"
            f"Кому: {AUDIENCES[parts[0]]}\n"
            f"Сейчас получателей: {count}\n"
            f"Когда: {send_at.astimezone(config.timezone).strftime('%d.%m.%Y %H:%M')} ({config.timezone.key})\n\n"
            "Проверьте копию выше. После подтверждения пост попадёт в очередь. "
            "Состав получателей фиксируется при подтверждении; перед отправкой проверяются отписки. "
            "Не удаляйте копию поста до завершения рассылки.",
            reply_markup=confirm_keyboard(job_id),
        )

    @admin.callback_query(F.data.startswith("job:"))
    async def job_action(query: CallbackQuery, db: Database):
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[1] not in {"confirm", "cancel"} or not parts[2].isdigit():
            await query.answer("Неизвестное действие")
            return
        job_id = int(parts[2])
        if parts[1] == "confirm":
            changed = db.confirm_job(job_id)
            text = f"Рассылка #{job_id} подтверждена. Результат появится здесь и в /jobs."
        else:
            changed = db.cancel_job(job_id)
            text = f"Рассылка #{job_id} отменена."
        await query.answer("Готово" if changed else "Эта рассылка уже обработана")
        if changed:
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer(text)

    @admin.message(Command("jobs"))
    async def jobs(message: Message, db: Database):
        rows = db.recent_jobs()
        if not rows:
            await message.answer("Рассылок пока нет. Инструкция: /admin")
            return
        labels = {"draft": "черновик", "pending": "в очереди", "running": "отправляется",
                  "done": "завершена", "failed": "ошибка", "cancelled": "отменена"}
        blocks = []
        for job in rows:
            counts = db.job_counts(job["id"])
            blocks.append(
                f"<b>#{job['id']} · {labels.get(job['state'], job['state'])}</b> · {job['audience']}\n"
                f"{local_time(job['send_at'], config.timezone)}\n"
                f"отправлено {counts['sent']}, пропущено {counts['skipped']}, "
                f"ошибок {counts['failed']}, в очереди {counts['pending']}"
                + ("\n" + escape(job["error"]) if job["error"] else "")
            )
        await message.answer("\n\n".join(blocks) + f"\n\nВремя: {config.timezone.key}\nОтменить: /cancel ID")

    @admin.message(Command("cancel"))
    async def cancel(message: Message, command: CommandObject, db: Database):
        try:
            job_id = int(command.args or "")
        except ValueError:
            await message.answer("Пример: /cancel 12")
            return
        changed = db.cancel_job(job_id)
        await message.answer(
            f"Рассылка #{job_id} остановлена. Уже отправленные сообщения остаются у получателей."
            if changed else "Не найдена активная рассылка с таким ID. Проверьте /jobs."
        )

    @public.message(CommandStart())
    async def start(message: Message):
        await message.answer(content.WELCOME, reply_markup=welcome_keyboard())

    @public.callback_query(F.data == "lesson:show")
    async def lesson(query: CallbackQuery, bot: Bot, db: Database):
        await query.answer()
        chat_id = query.from_user.id
        await bot.send_photo(chat_id, FSInputFile(LESSON_PHOTO))
        await bot.send_message(chat_id, content.LESSON, reply_markup=lesson_keyboard())
        db.schedule_lesson_poll(chat_id)

    @public.message(Command("club"))
    async def club(message: Message, bot: Bot, db: Database):
        await join_club(bot, message.chat.id, db, config)

    @public.callback_query(F.data == "club:join")
    async def join(query: CallbackQuery, bot: Bot, db: Database):
        await query.answer()
        await join_club(bot, query.from_user.id, db, config)

    @public.message(Command("guide"))
    async def guide(message: Message, bot: Bot):
        await show_guide(bot, message.chat.id)

    @public.callback_query(F.data == "guide:show")
    async def guide_callback(query: CallbackQuery, bot: Bot):
        await query.answer()
        await show_guide(bot, query.from_user.id)

    @public.message(Command("profile"))
    async def profile(message: Message, bot: Bot, db: Database):
        await send_profile(bot, message.chat.id, db, summary=True)

    @public.callback_query(F.data == "profile:show")
    async def profile_callback(query: CallbackQuery, bot: Bot, db: Database):
        await query.answer()
        await send_profile(bot, query.from_user.id, db, summary=True)

    @public.callback_query(F.data.startswith("role:") | F.data.in_({"training:on", "training:off"}))
    async def choose(query: CallbackQuery, bot: Bot, db: Database):
        if query.data.startswith("role:"):
            role = query.data.split(":", 1)[1]
            if role not in content.ROLES:
                await query.answer("Выбери вариант на кнопке")
                return
            db.set_role(query.from_user.id, role)
        else:
            db.set_interest(query.from_user.id, query.data == "training:on")
        await query.answer()
        row = db.user(query.from_user.id)
        if row:
            try:
                await query.message.edit_reply_markup(reply_markup=profile_keyboard(row["role"], bool(row["training_interest_at"])))
            except TelegramBadRequest:
                pass  # Повторное нажатие того же ответа не меняет сообщение.
        await bot.send_message(query.from_user.id, content.ACK)

    @public.message(Command("stop"))
    async def stop(message: Message, db: Database):
        db.set_notifications(message.from_user.id, False)
        await message.answer("сообщения отключены ✦ вернуться в зал можно в любой момент: /club")

    @public.message(Command("help"))
    async def help_message(message: Message):
        text = (
            "/club — вступить в киноклуб или включить сообщения\n"
            "/guide — как смотреть кино с заметками\n"
            "/profile — кто ты в кино и интерес к обучению\n"
            "/stop — отключить сообщения"
        )
        if message.from_user.id in config.admin_ids:
            text += "\n/admin — управление и статистика"
        await message.answer(text)

    @public.message()
    async def fallback(message: Message):
        if message.from_user and message.from_user.id in config.admin_ids:
            await message.answer("Чтобы сделать рассылку, ответьте на свой пост командой /post club. Другие команды: /admin")
        else:
            await message.answer("я рядом 🎬 начать — /start, киноклуб — /club, подсказки — /help")

    @public.callback_query()
    async def unknown_callback(query: CallbackQuery):
        await query.answer("Эта кнопка устарела. Открой /start")

    @root.my_chat_member(F.chat.type == "private")
    async def blocked(event: ChatMemberUpdated, db: Database):
        db.set_blocked(event.chat.id, event.new_chat_member.status == "kicked")

    @root.errors()
    async def handle_error(event: ErrorEvent):
        log.error("Ошибка обработки обновления: %s", type(event.exception).__name__)
        # Не выводим токен, личные данные или исходный Update в журнал.
        if not isinstance(event.exception, TelegramAPIError):
            message = event.update.message
            if message and message.chat.type == "private":
                try:
                    await message.answer("не получилось с первого раза. попробуй ещё раз через минуту ✦")
                except TelegramAPIError:
                    pass
        return True

    return root
