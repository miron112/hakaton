"""YandexGPT — заглушка."""

from radar.config import Settings
from radar.llm.base import LLMClient, LLMReply, Message


class YandexGPTClient(LLMClient):
    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model

    async def _chat(self, messages: list[Message]) -> LLMReply:
        # TODO: вызов Foundation Models API (нужны folder_id и IAM-токен или API-ключ)
        raise NotImplementedError("YandexGPT не реализован: используйте LLM_PROVIDER=openai_compat")
