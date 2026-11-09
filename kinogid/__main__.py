import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeChat, LinkPreviewOptions

from . import content
from .config import Config
from .db import Database
from .handlers import TrackingMiddleware, build_router
from .worker import broadcast_worker, lesson_worker

log = logging.getLogger("kinogid")


async def setup_profile(bot: Bot, config: Config) -> None:
    commands = [
        BotCommand(command="start", description="Зайти в зал 🎬"),
        BotCommand(command="club", description="Вступить в киноклуб / включить сообщения"),
        BotCommand(command="guide", description="Как смотреть кино с заметками 🍿"),
        BotCommand(command="profile", description="Кто ты в кино / интерес к обучению"),
        BotCommand(command="stop", description="Отключить сообщения"),
        BotCommand(command="help", description="Помощь"),
    ]
    for language in ("", "ru"):
        await bot.set_my_description(description=content.DESCRIPTION, language_code=language)
        await bot.set_my_short_description(short_description=content.SHORT_DESCRIPTION, language_code=language)
        await bot.set_my_commands(commands, language_code=language)
        for admin_id in config.admin_ids:
            try:
                await bot.set_my_commands(
                    commands + [BotCommand(command="admin", description="Управление и статистика")],
                    scope=BotCommandScopeChat(chat_id=admin_id), language_code=language,
                )
            except TelegramAPIError:
                # Администратор ещё мог не открыть бота; /admin всё равно работает.
                log.warning("Не удалось настроить меню администратора %s", admin_id)


async def main() -> None:
    config = Config.from_env()
    bot = Bot(config.token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview=LinkPreviewOptions(is_disabled=True),
    ))
    db = Database(config.db_path, config.admin_ids)
    workers = []
    try:
        me = await bot.get_me()
        log.info("Запуск @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            await setup_profile(bot, config)
        except TelegramAPIError:
            log.warning("Не удалось обновить описание/меню. Бот продолжает запуск.")
        dispatcher = Dispatcher(db=db, config=config)
        dispatcher.message.outer_middleware(TrackingMiddleware())
        dispatcher.callback_query.outer_middleware(TrackingMiddleware())
        dispatcher.include_router(build_router(config))
        workers = [
            asyncio.create_task(broadcast_worker(bot, db)),
            asyncio.create_task(lesson_worker(bot, db)),
        ]
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            handle_as_tasks=False,
            close_bot_session=False,
        )
    finally:
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with suppress(asyncio.CancelledError):
                await worker
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил токен. Проверьте BOT_TOKEN в .env.")
        raise SystemExit(1)
    except ValueError as exc:
        log.error("Ошибка настройки: %s", exc)
        raise SystemExit(1)
    except Exception as exc:
        log.error("Не удалось запустить бота (%s). Проверьте настройки, сеть и единственность процесса.", type(exc).__name__)
        raise SystemExit(1)
