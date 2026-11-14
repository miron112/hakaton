from radar.config import Settings, get_settings
from radar.llm.base import LLMClient, LLMReply, LLMResponseError, LLMResult, LLMUsage


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Создаёт клиента по LLM_PROVIDER. Импорты внутри, чтобы не тянуть лишние SDK."""
    settings = settings or get_settings()
    provider = settings.llm_provider.lower()
    if provider == "openai_compat":
        from radar.llm.openai_compat import OpenAICompatClient

        return OpenAICompatClient(settings)
    if provider == "yandex":
        from radar.llm.yandex import YandexGPTClient

        return YandexGPTClient(settings)
    if provider == "gigachat":
        from radar.llm.gigachat import GigaChatClient

        return GigaChatClient(settings)
    raise ValueError(f"неизвестный LLM_PROVIDER: {settings.llm_provider!r}")


__all__ = ["LLMClient", "LLMReply", "LLMResponseError", "LLMResult", "LLMUsage", "get_llm_client"]
