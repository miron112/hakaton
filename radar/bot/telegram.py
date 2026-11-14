"""Обёртка над aiogram: создание бота и отправка сообщений в чат редактора."""

import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
)

from radar.bot.notifier import ACTION_LABELS
from radar.config import Settings
from radar.log import log_event

CALLBACK_PREFIX = "fb"


def create_bot(settings: Settings) -> Bot:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан: добавьте токен бота в .env")
    return Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(
            parse_mode="HTML", link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )


async def prepare_polling(bot: Bot, settings: Settings) -> bool:
    """Проверяет, что бот может получать сообщения опросом. False — мешает чужой вебхук."""
    info = await bot.get_webhook_info()
    if not info.url:
        return True
    if settings.telegram_delete_webhook:
        await bot.delete_webhook()
        log_event("bot", "webhook_deleted", level=logging.WARNING)
        return True
    log_event(
        "bot", "webhook_active", level=logging.ERROR,
        hint="у бота включён вебхук: им пользуется другой сервис. Создайте отдельного бота "
             "или задайте TELEGRAM_DELETE_WEBHOOK=true (это отключит бота от того сервиса)",
    )
    return False


def feedback_keyboard(event_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"{CALLBACK_PREFIX}:{action}:{event_id}")
        for action, label in ACTION_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


class TelegramSender:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        if settings.telegram_editor_chat_id is None:
            raise RuntimeError(
                "TELEGRAM_EDITOR_CHAT_ID не задан: напишите боту /start — он пришлёт id чата"
            )
        self._bot = bot
        self._chat_id = settings.telegram_editor_chat_id

    async def _send(self, text: str, **kwargs) -> int:
        try:
            message = await self._bot.send_message(self._chat_id, text, **kwargs)
        except TelegramRetryAfter as error:  # лимит Telegram: ждём, сколько просят, и повторяем
            await asyncio.sleep(error.retry_after + 1)
            message = await self._bot.send_message(self._chat_id, text, **kwargs)
        return message.message_id

    @staticmethod
    def _reply(reply_to: int | None) -> ReplyParameters | None:
        if not reply_to:
            return None
        return ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)

    async def send_event(self, text: str, event_id: int, reply_to: int | None = None) -> int:
        return await self._send(
            text, reply_markup=feedback_keyboard(event_id), reply_parameters=self._reply(reply_to)
        )

    async def send_text(self, text: str, reply_to: int | None = None) -> int:
        return await self._send(text, reply_parameters=self._reply(reply_to))
