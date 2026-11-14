"""Только бот (кнопки и команды), без сбора: python -m radar.bot"""

import asyncio

from aiogram import Dispatcher

from radar.bot.handlers import router
from radar.bot.telegram import create_bot, prepare_polling
from radar.config import get_settings
from radar.db import init_db
from radar.log import setup_logging


async def run_bot() -> None:
    settings = get_settings()
    bot = create_bot(settings)
    try:
        if not await prepare_polling(bot, settings):
            print("Бот не запущен: на нём включён вебхук другого сервиса (подробности в логе выше).")
            return
        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        me = await bot.get_me()
        print(f"Бот @{me.username} запущен. Остановить: Ctrl+C")
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    setup_logging(get_settings().log_level)
    init_db()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
