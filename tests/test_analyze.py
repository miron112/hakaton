"""Предфильтр и сохранение результатов анализа (LLM подменён)."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, select

from radar.config import get_settings
from radar.db import init_db, make_engine, session_scope
from radar.llm.base import LLMClient, LLMReply, Message
from radar.models import Analysis, RawItem, Source, utcnow
from radar.pipeline.analyze import analyze_pending
from radar.pipeline.prefilter import prefilter_new, reject_reason
from radar.prompts import ANALYZE_PROMPT_VERSION
from tests.test_llm import VALID

LONG_TEXT = "В Сургуте на улице Ленина произошла авария на теплотрассе, движение ограничено."


class RoutingClient(LLMClient):
    """Выбирает ответ по маркеру в тексте материала."""

    model = "test-model"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def _chat(self, messages: list[Message]) -> LLMReply:
        user = messages[1]["content"]
        self.prompts.append(user)
        if "СБОЙ" in user:
            raise ConnectionError("нет связи с провайдером")
        if "МУСОР" in user:
            return LLMReply("это не JSON")
        return LLMReply(json.dumps(VALID, ensure_ascii=False), prompt_tokens=500, completion_tokens=100)


@pytest.fixture
def engine(tmp_path) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        session.add(
            Source(name="Канал", type="telegram", url="chan", geo="surgut_city", note="официальный")
        )
    return engine


def _add_item(engine: Engine, key: str, text: str = LONG_TEXT, age_hours: float = 1) -> None:
    with session_scope(engine) as session:
        session.add(
            RawItem(
                source_id=1, external_id=key, url=f"https://t.me/chan/{key}", text=f"{text} {key}",
                published_at=utcnow() - timedelta(hours=age_hours), content_hash=key,
            )
        )


def _statuses(engine: Engine) -> dict[str, str]:
    with session_scope(engine) as session:
        return {row.external_id: row.status for row in session.scalars(select(RawItem))}


def test_reject_reasons(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "stop_words", "Розыгрыш, промокод")
    now = datetime(2026, 11, 14, 12, 0)

    assert reject_reason(None, LONG_TEXT, now, settings, now) is None
    assert reject_reason(None, LONG_TEXT, None, settings, now) is None  # без даты — пропускаем
    assert reject_reason("Коротко", "", now, settings, now) == "too_short"
    assert reject_reason(None, LONG_TEXT, now - timedelta(hours=100), settings, now) == "too_old"
    assert reject_reason(None, LONG_TEXT + " РОЗЫГРЫШ призов", now, settings, now) == "stop_word"
    # География предфильтром не решается: текст без слова «Сургут» проходит
    assert reject_reason(None, "В Лянторе открыли новую школу на тысячу мест, занятия с января.", now, settings, now) is None


def test_prefilter_marks_rejected_items(engine: Engine) -> None:
    _add_item(engine, "ok")
    _add_item(engine, "old", age_hours=500)
    _add_item(engine, "short", text="Фото")

    stats = prefilter_new(engine)

    assert (stats.checked, stats.passed) == (3, 1)
    assert stats.reasons == {"too_old": 1, "too_short": 1}
    assert _statuses(engine) == {"ok": "new", "old": "filtered_out", "short": "filtered_out"}


def test_analysis_is_saved_with_model_and_prompt_version(engine: Engine) -> None:
    _add_item(engine, "1")
    client = RoutingClient()

    stats = asyncio.run(analyze_pending(client, engine))

    assert (stats.analyzed, stats.invalid, stats.failed) == (1, 0, 0)
    assert (stats.usage.prompt_tokens, stats.usage.completion_tokens) == (500, 100)
    with session_scope(engine) as session:
        analysis = session.scalar(select(Analysis))
        assert analysis.model == "test-model" and analysis.prompt_version == ANALYZE_PROMPT_VERSION
        assert analysis.result["headline"] == VALID["headline"]
        assert analysis.raw_item.status == "analyzed"
    prompt = client.prompts[0]
    assert "ИСТОЧНИК: Канал (telegram; приоритет источника: 2; официальный)" in prompt
    assert "https://t.me/chan/1" in prompt and LONG_TEXT in prompt


def test_one_bad_item_does_not_affect_others(engine: Engine) -> None:
    _add_item(engine, "good")
    _add_item(engine, "garbage", text=LONG_TEXT + " МУСОР")
    _add_item(engine, "outage", text=LONG_TEXT + " СБОЙ")

    stats = asyncio.run(analyze_pending(RoutingClient(), engine))

    assert (stats.analyzed, stats.invalid, stats.failed) == (1, 1, 1)
    # Невалидный JSON — окончательная ошибка; сбой API — материал ждёт следующего цикла
    assert _statuses(engine) == {"good": "analyzed", "garbage": "error", "outage": "new"}


def test_cycle_limit_postpones_the_rest(engine: Engine, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "llm_max_items_per_cycle", 2)
    for key in ("a", "b", "c"):
        _add_item(engine, key)

    stats = asyncio.run(analyze_pending(RoutingClient(), engine))

    assert (stats.analyzed, stats.postponed) == (2, 1)
    assert sorted(_statuses(engine).values()) == ["analyzed", "analyzed", "new"]


def test_already_analyzed_items_are_not_sent_again(engine: Engine) -> None:
    _add_item(engine, "1")
    client = RoutingClient()
    asyncio.run(analyze_pending(client, engine))
    asyncio.run(analyze_pending(client, engine))
    assert len(client.prompts) == 1
