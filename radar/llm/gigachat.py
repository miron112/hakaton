"""GigaChat — заглушка."""

from radar.config import Settings
from radar.llm.base import LLMClient, LLMReply, Message


class GigaChatClient(LLMClient):
    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model

    async def _chat(self, messages: list[Message]) -> LLMReply:
        # TODO: OAuth-токен по ключу авторизации, затем POST /chat/completions
        raise NotImplementedError("GigaChat не реализован: используйте LLM_PROVIDER=openai_compat")
