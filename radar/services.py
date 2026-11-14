"""Сборка внешних сервисов цикла. Чего нет в .env — того шага не будет, с понятной причиной."""

from dataclasses import dataclass, field

from aiogram import Bot

from radar.bot.notifier import Sender
from radar.bot.telegram import TelegramSender, create_bot
from radar.config import get_settings
from radar.llm import LLMClient, get_llm_client
from radar.pipeline.embeddings import Embedder, get_embedder


@dataclass
class Services:
    client: LLMClient | None = None
    embedder: Embedder | None = None
    bot: Bot | None = None
    sender: Sender | None = None
    skip_reasons: dict[str, str] = field(default_factory=dict)

    async def close(self) -> None:
        if self.bot is not None:
            await self.bot.session.close()


def build_services() -> Services:
    settings = get_settings()
    services = Services()

    try:
        services.client = get_llm_client(settings)
    except (RuntimeError, ValueError) as error:
        services.skip_reasons["analyze"] = str(error)
        services.skip_reasons["dedup"] = "нет LLM-клиента"

    if services.client is not None:
        try:
            services.embedder = get_embedder()
        except Exception as error:  # нет sentence-transformers или не скачалась модель
            services.skip_reasons["dedup"] = f"эмбеддинги недоступны: {type(error).__name__}: {error}"[:300]

    try:
        services.bot = create_bot(settings)
    except RuntimeError as error:
        services.skip_reasons["notify"] = str(error)
    if services.bot is not None:
        try:
            services.sender = TelegramSender(services.bot, settings)
        except RuntimeError as error:
            services.skip_reasons["notify"] = str(error)
    return services
