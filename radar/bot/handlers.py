"""Команды бота и колбэки кнопок обратной связи."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from radar.bot.notifier import ACTION_LABELS, save_feedback
from radar.bot.reports import build_status_text, build_today_text
from radar.bot.telegram import CALLBACK_PREFIX, feedback_keyboard
from radar.config import get_settings
from radar.pipeline import cycle

router = Router()


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Новостной радар Сургута и Югры.\n"
        "Присылаю значимые новости из отслеживаемых источников.\n\n"
        f"ID этого чата: <code>{message.chat.id}</code>\n"
        "Укажите его в .env как TELEGRAM_EDITOR_CHAT_ID, чтобы получать уведомления здесь.\n\n"
        "Команды: /status — состояние источников и последний цикл, /today — сводка за день."
    )


def _is_editor_chat(message: Message) -> bool:
    editor_chat = get_settings().telegram_editor_chat_id
    return editor_chat is None or message.chat.id == editor_chat


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    if not _is_editor_chat(message):
        await message.answer("Команда доступна только в чате редакции.")
        return
    await message.answer(build_status_text(cycle.state, cycle.state.next_run_at))


@router.message(Command("today"))
async def on_today(message: Message) -> None:
    if not _is_editor_chat(message):
        await message.answer("Команда доступна только в чате редакции.")
        return
    await message.answer(build_today_text())


def parse_callback(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in ACTION_LABELS:
        return None
    return (parts[1], int(parts[2])) if parts[2].isdigit() else None


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def on_feedback(callback: CallbackQuery) -> None:
    parsed = parse_callback(callback.data)
    if parsed is None:
        await callback.answer("Не понял кнопку")
        return
    action, event_id = parsed
    text = save_feedback(event_id, callback.from_user.id, callback.from_user.full_name, action)
    if text is None:
        await callback.answer("Событие не найдено")
        return
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=feedback_keyboard(event_id))
        except TelegramBadRequest:
            pass  # текст не изменился (повторное нажатие в ту же минуту)
    await callback.answer(ACTION_LABELS[action])
