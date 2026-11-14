"""Разбор и валидация ответов LLM, повтор при невалидном JSON."""

import asyncio
import json

import pytest

from radar.config import get_settings
from radar.llm.base import LLMClient, LLMReply, LLMResponseError, Message, extract_json
from radar.pipeline.analyze import AnalysisResult, build_system_prompt, normalize
from radar.prompts import render

VALID = {
    "is_relevant": True,
    "geo_level": "surgut_city",
    "affects_surgut": True,
    "places": ["Сургут", "5 микрорайон"],
    "topic": "ЖКХ",
    "content_type": "event",
    "importance": 4,
    "importance_reason": "Отключение воды в двух микрорайонах.",
    "urgency": "breaking",
    "headline": "В 5 и 5А микрорайонах Сургута отключили холодную воду",
    "summary": "Из-за аварии на сетях воды не будет до 18:00.",
    "key_facts": ["до 18:00"],
    "entities": ["Горводоканал"],
    "needs_verification": False,
    "verification_note": None,
    "confidence": 0.9,
}


class ScriptedClient(LLMClient):
    """Отдаёт заранее заданные ответы и запоминает запросы."""

    model = "test-model"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    async def _chat(self, messages: list[Message]) -> LLMReply:
        self.calls.append(messages)
        return LLMReply(self.replies.pop(0), prompt_tokens=100, completion_tokens=20)


def _complete(client: ScriptedClient):
    return asyncio.run(client.complete_json("system", "user", AnalysisResult))


def test_valid_answer_is_accepted() -> None:
    client = ScriptedClient(json.dumps(VALID, ensure_ascii=False))
    result = _complete(client)

    assert result.data["importance"] == 4 and result.data["geo_level"] == "surgut_city"
    assert (result.usage.requests, result.usage.prompt_tokens) == (1, 100)


def test_json_wrapped_in_markdown_is_accepted() -> None:
    client = ScriptedClient(f"Вот ответ:\n```json\n{json.dumps(VALID, ensure_ascii=False)}\n```")
    assert _complete(client).data["headline"] == VALID["headline"]


def test_invalid_answer_is_retried_once_with_error_text() -> None:
    broken = {**VALID, "importance": 9}
    client = ScriptedClient(json.dumps(broken), json.dumps(VALID))
    result = _complete(client)

    assert result.data["importance"] == 4
    assert len(client.calls) == 2 and result.usage.requests == 2
    retry_messages = client.calls[1]
    assert retry_messages[2]["role"] == "assistant"
    assert "importance" in retry_messages[3]["content"]


def test_two_invalid_answers_raise() -> None:
    client = ScriptedClient("не могу ответить", '{"is_relevant": "может быть"}')
    with pytest.raises(LLMResponseError):
        _complete(client)
    assert len(client.calls) == 2


def test_missing_optional_fields_get_defaults() -> None:
    minimal = {
        key: VALID[key]
        for key in ("is_relevant", "geo_level", "content_type", "importance", "headline", "summary")
    }
    result = AnalysisResult.model_validate(minimal)
    assert result.key_facts == [] and result.urgency == "background"


@pytest.mark.parametrize("text", ["", "нет json", "[1, 2]", '{"a": 1'])
def test_extract_json_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json(text)


def test_low_value_types_are_capped_and_topic_is_normalized() -> None:
    settings = get_settings()
    data = normalize({**VALID, "content_type": "routine_pr", "importance": 5, "topic": "жкх"}, settings)
    assert data["importance"] == 2 and data["topic"] == "ЖКХ"

    data = normalize({**VALID, "topic": "Космос", "headline": "Два\nслова"}, settings)
    assert data["topic"] == "другое" and data["importance"] == 4
    assert data["headline"] == "Два слова"


def test_system_prompt_has_no_unfilled_variables() -> None:
    prompt = build_system_prompt(get_settings())
    assert "{{" not in prompt and "ЖКХ" in prompt


def test_render_does_not_expand_variables_inside_values() -> None:
    assert render("Текст: {{text}}", text="{{url}} внутри поста") == "Текст: {{url}} внутри поста"
    with pytest.raises(KeyError):
        render("{{unknown}}")
