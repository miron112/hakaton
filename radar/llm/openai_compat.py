"""Клиент для любого OpenAI-совместимого API: Polza.ai, OpenRouter, локальные модели."""

from typing import Any

from openai import AsyncOpenAI

from radar.config import Settings
from radar.llm.base import LLMClient, LLMReply, Message


class OpenAICompatClient(LLMClient):
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY не задан: добавьте ключ в .env")
        self.model = settings.llm_model
        self._temperature = settings.llm_temperature
        self._json_mode = settings.llm_json_mode
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )

    async def _chat(self, messages: list[Message]) -> LLMReply:
        extra: dict[str, Any] = {}
        if self._json_mode:
            extra["response_format"] = {"type": "json_object"}
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self._temperature,
            **extra,
        )
        usage = response.usage
        cost = (usage.model_extra or {}).get("cost") if usage else None
        return LLMReply(
            text=response.choices[0].message.content or "",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cost=float(cost) if isinstance(cost, (int, float)) else None,
        )
