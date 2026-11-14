"""Подмены внешних сервисов для тестов."""

import json
import math

from radar.llm.base import LLMClient, LLMReply, Message


class FakeSender:
    def __init__(self, fail_on: str | None = None) -> None:
        self.events: list[tuple[int, str]] = []
        self.texts: list[tuple[str, int | None]] = []
        self.fail_on = fail_on
        self._next_id = 100

    async def send_event(self, text: str, event_id: int, reply_to: int | None = None) -> int:
        if self.fail_on and self.fail_on in text:
            raise ConnectionError("telegram недоступен")
        self.events.append((event_id, text))
        self._next_id += 1
        return self._next_id

    async def send_text(self, text: str, reply_to: int | None = None) -> int:
        self.texts.append((text, reply_to))
        self._next_id += 1
        return self._next_id


class KeywordEmbedder:
    """Тексты с одним и тем же ключевым словом получают почти одинаковые векторы."""

    def __init__(self, *keywords: str) -> None:
        self.keywords = keywords

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * (len(self.keywords) + 1)
            lowered = text.lower()
            index = next((i for i, word in enumerate(self.keywords) if word in lowered), None)
            if index is None:
                vector[-1] = 1.0  # тексты без ключевых слов похожи только друг на друга
            else:
                vector[index] = 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append([value / norm for value in vector])
        return vectors


class DedupClient(LLMClient):
    """Отвечает на Промт 2 заранее заданными решениями."""

    model = "test-model"

    def __init__(self, *answers: dict | str | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def _chat(self, messages: list[Message]) -> LLMReply:
        self.prompts.append(messages[1]["content"])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return LLMReply(text, prompt_tokens=300, completion_tokens=40)
