"""Абстрактный LLM-клиент: запрос → JSON, проверенный pydantic-схемой."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

Message = dict[str, str]

_RETRY_INSTRUCTION = (
    "Твой предыдущий ответ не прошёл проверку: {error}\n"
    "Верни исправленный ответ: строго один JSON-объект по схеме, без пояснений и без markdown."
)


class LLMResponseError(Exception):
    """Модель ответила, но ответ не удалось привести к схеме даже после повтора."""


@dataclass
class LLMReply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None  # если провайдер сообщает стоимость запроса


@dataclass
class LLMUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0

    def add(self, reply: LLMReply) -> None:
        self.requests += 1
        self.prompt_tokens += reply.prompt_tokens
        self.completion_tokens += reply.completion_tokens
        self.cost += reply.cost or 0.0


@dataclass
class LLMResult:
    data: dict[str, Any]
    usage: LLMUsage


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа, даже если модель обернула его в ```json или текст."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    return data


def _short_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in error.errors()
        )[:600]
    return str(error)[:600]


class LLMClient(ABC):
    model: str

    @abstractmethod
    async def _chat(self, messages: list[Message]) -> LLMReply:
        """Один запрос к провайдеру. Ошибки сети и API пробрасываются наружу."""

    async def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> LLMResult:
        """Возвращает ответ, проверенный схемой. Невалидный ответ — один повтор с текстом ошибки."""
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        usage = LLMUsage()
        error: Exception | None = None
        for _ in range(2):
            reply = await self._chat(messages)
            usage.add(reply)
            try:
                validated = schema.model_validate(extract_json(reply.text))
                return LLMResult(validated.model_dump(), usage)
            except (ValueError, ValidationError) as caught:
                error = caught
                messages = messages[:2] + [
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": _RETRY_INSTRUCTION.format(error=_short_error(caught))},
                ]
        raise LLMResponseError(_short_error(error) if error else "неизвестная ошибка")
