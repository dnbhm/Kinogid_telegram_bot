import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from .db import Database
from .handlers import send_profile, show_guide

log = logging.getLogger(__name__)


async def deliver_lesson_followup(bot: Bot, db: Database, followup) -> None:
    user_id = followup["user_id"]
    step = followup["step"]
    if followup["notifications"] == 0 or followup["blocked"]:
        db.update_lesson_followup(user_id, "done")
        return
    try:
        if step == "poll":
            await send_profile(bot, user_id, db)
        else:
            await show_guide(bot, user_id)
    except TelegramForbiddenError:
        db.set_blocked(user_id, True)
        db.update_lesson_followup(user_id, "done")
    except TelegramBadRequest:
        log.warning("Telegram отклонил сообщение сценария урока: %s", step)
        db.update_lesson_followup(user_id, "done")
    except TelegramRetryAfter as exc:
        db.update_lesson_followup(user_id, step, timedelta(seconds=max(exc.retry_after, 1)))
    except TelegramAPIError:
        log.warning("Не удалось отправить сообщение сценария урока: %s", step)
        db.update_lesson_followup(user_id, step, timedelta(minutes=1))
    else:
        if step == "poll":
            db.update_lesson_followup(user_id, "guide", timedelta(minutes=1))
        else:
            db.update_lesson_followup(user_id, "done")


async def lesson_worker(bot: Bot, db: Database) -> None:
    while True:
        followup = db.next_lesson_followup()
        if followup:
            await deliver_lesson_followup(bot, db, followup)
        await asyncio.sleep(0.1 if followup else 1)


async def deliver_job(bot: Bot, db: Database, job) -> None:
    job_id = job["id"]
    while True:
        current = db.job(job_id)
        if not current or current["state"] != "running":
            return
        user_id = db.next_recipient(job_id)
        if user_id is None:
            db.finish_job(job_id)
            return
        if not db.eligible(user_id, job["audience"]):
            db.delivery_result(job_id, user_id, "skipped")
            continue
        attempts = db.attempt(job_id, user_id)
        try:
            await bot.copy_message(
                chat_id=user_id, from_chat_id=job["source_chat_id"],
                message_id=job["source_message_id"],
            )
        except TelegramRetryAfter as exc:
            # Лимит Telegram действует на бота; ждём, не помечая адресата доставленным.
            await asyncio.sleep(max(exc.retry_after, 1))
            continue
        except TelegramForbiddenError:
            db.set_blocked(user_id, True)
            db.delivery_result(job_id, user_id, "skipped", "Telegram запретил отправку")
        except (TelegramNetworkError, TelegramServerError):
            if attempts < 5:
                await asyncio.sleep(min(2 ** attempts, 30))
                continue
            db.delivery_result(job_id, user_id, "failed", "Сбой сети или Telegram: 5 попыток")
        except TelegramBadRequest as exc:
            reason = exc.message.lower()
            if any(part in reason for part in (
                "message to copy not found", "message can't be copied", "message_id_invalid",
                "message identifier is not specified",
            )):
                db.finish_job(job_id, "Копия поста недоступна. Создайте рассылку заново.")
                return
            db.delivery_result(job_id, user_id, "failed", "Telegram отклонил сообщение")
        except TelegramAPIError:
            db.delivery_result(job_id, user_id, "failed", "Ошибка Telegram API")
        else:
            db.delivery_result(job_id, user_id, "sent")
        # Оставляем запас под ответы бота; платные ускоренные рассылки не используются.
        await asyncio.sleep(0.08)


async def broadcast_worker(bot: Bot, db: Database) -> None:
    while True:
        job = db.next_job()
        if not job:
            await asyncio.sleep(2)
            continue
        try:
            await deliver_job(bot, db, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Не пишем содержимое постов или токен в журнал.
            log.error("Ошибка рассылки %s: %s", job["id"], type(exc).__name__)
            db.finish_job(job["id"], "Внутренняя ошибка. Проверьте журнал сервера.")
        result = db.job(job["id"])
        counts = db.job_counts(job["id"])
        text = (
            f"Рассылка #{job['id']}: {result['state']}\n"
            f"Отправлено: {counts['sent']}; пропущено: {counts['skipped']}; "
            f"ошибок: {counts['failed']}; осталось: {counts['pending']}."
        )
        if result["error"]:
            text += "\n" + result["error"]
        try:
            await bot.send_message(job["admin_id"], text, parse_mode=None)
        except (TelegramAPIError, OSError):
            log.warning("Не удалось уведомить администратора о рассылке %s", job["id"])
