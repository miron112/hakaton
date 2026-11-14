"""Полный цикл на подменённых сервисах и тексты /status, /today."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import Engine

from radar.bot.notifier import save_feedback
from radar.bot.reports import build_status_text, build_today_text
from radar.collectors.base import CollectedItem, Collector
from radar.config import get_settings
from radar.db import init_db, make_engine, session_scope
from radar.llm.base import LLMClient, LLMReply, Message
from radar.models import RawItem, Source, utcnow
from radar.pipeline import collect, cycle
from tests.fakes import FakeSender, KeywordEmbedder
from tests.test_llm import VALID


class FeedCollector(Collector):
    feeds: dict[str, list[CollectedItem] | Exception] = {}

    async def fetch(self, source: Source, known_ids: frozenset[str] = frozenset()) -> list[CollectedItem]:
        feed = self.feeds[source.url]
        if isinstance(feed, Exception):
            raise feed
        return feed


class PipelineClient(LLMClient):
    """Промт 1: важность по маркеру в тексте. Промт 2: всегда duplicate первого кандидата."""

    model = "test-model"

    async def _chat(self, messages: list[Message]) -> LLMReply:
        user = messages[1]["content"]
        if user.startswith("НОВЫЙ МАТЕРИАЛ"):
            event_id = user.split("[", 1)[1].split("]", 1)[0]
            return LLMReply(json.dumps({"relation": "duplicate", "event_id": event_id}))
        headline = user.split("<<<\n", 1)[1].split("\n", 1)[0]
        relevant = "ШУМ" not in user
        return LLMReply(json.dumps({**VALID, "headline": headline, "is_relevant": relevant,
                                    "importance": 4 if relevant else 1}, ensure_ascii=False))


def _item(key: str, title: str) -> CollectedItem:
    return CollectedItem(key, f"https://example.com/{key}", title, f"Подробный текст материала {key} для анализа.", utcnow())


@pytest.fixture
def engine(tmp_path, monkeypatch) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    monkeypatch.setattr(collect, "COLLECTORS", {"rss": FeedCollector})
    monkeypatch.setattr(get_settings(), "fetch_retry_pause_seconds", 0)
    monkeypatch.setattr("radar.bot.notifier.SEND_PAUSE_SECONDS", 0)
    monkeypatch.setattr(cycle, "state", cycle.CycleState())
    with session_scope(engine) as session:
        for name in ("a", "b", "bad"):
            session.add(Source(name=f"Источник {name}", type="rss", url=name, geo="surgut_city"))
    FeedCollector.feeds = {
        "a": [_item("a1", "Пожар на складе"), _item("a2", "ШУМ: гороскоп на завтра")],
        "b": [_item("b1", "На складе пожар, подробности")],
        "bad": RuntimeError("сайт лежит"),
    }
    return engine


def _run(engine: Engine, sender: FakeSender | None, **kwargs):
    return asyncio.run(cycle.run_cycle(PipelineClient(), KeywordEmbedder("пожар"), sender, engine, **kwargs))


def test_full_cycle_one_event_from_two_sources_is_sent_once(engine: Engine) -> None:
    sender = FakeSender()

    report = _run(engine, sender)

    assert report.error is None
    assert (report.new_items, report.failed_sources) == (3, 1)  # упавший источник не мешает
    assert report.analyze.analyzed == 3
    assert (report.dedup.new_events, report.dedup.duplicates) == (1, 1)
    assert (report.notify.sent, report.notify.failed) == (1, 0)
    assert len(sender.events) == 1 and "Пожар на складе" in sender.events[0][1]

    # Второй цикл: ничего нового, ничего не отправлено
    again = _run(engine, sender)
    assert (again.new_items, again.notify.sent) == (0, 0) and len(sender.events) == 1
    assert cycle.state.cycles_done == 2 and cycle.state.running is False


def test_cycle_works_without_llm_and_telegram(engine: Engine) -> None:
    report = asyncio.run(
        cycle.run_cycle(None, None, None, engine, skip_reasons={"analyze": "LLM_API_KEY не задан"})
    )
    assert report.new_items == 3 and report.analyze is None and report.notify is None
    assert report.skipped_steps == {"analyze": "LLM_API_KEY не задан"}


def test_cycle_survives_unexpected_error(engine: Engine, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("диск переполнен")

    monkeypatch.setattr(cycle, "analyze_pending", boom)
    report = _run(engine, FakeSender())

    assert "диск переполнен" in report.error and cycle.state.running is False
    assert cycle.state.last_report is report


def test_status_shows_failing_and_silent_sources(engine: Engine) -> None:
    _run(engine, FakeSender())
    with session_scope(engine) as session:  # источник «b» отвечает, но последний пост — три месяца назад
        for item in session.query(RawItem).filter(RawItem.source_id == 2):
            item.published_at = utcnow() - timedelta(days=90)

    text = build_status_text(cycle.state, utcnow() + timedelta(minutes=15), engine)

    assert "Источники: работают 2 из 3" in text
    assert "❌ Источник bad — ошибок подряд: 1" in text and "сайт лежит" in text
    assert "💤 Источник b — последняя публикация" in text
    assert "новых материалов 3, событий 1, отправлено 1" in text
    assert "Следующий цикл:" in text
    assert "Сегодня: событий 1, уведомлений 1" in text


def test_status_before_first_cycle(engine: Engine) -> None:
    text = build_status_text(cycle.CycleState(), engine=engine)
    assert "Последний цикл: ещё не было" in text and "⏳ Источник a — ещё не опрашивался" in text


def test_today_summary(engine: Engine) -> None:
    assert "пока нет" in build_today_text(engine)

    _run(engine, FakeSender())
    save_feedback(1, 555, "Анна", "take", engine=engine)
    text = build_today_text(engine)

    assert "Собрано материалов 3, проанализировано 3" in text
    assert "Событий 1, уведомлений 1" in text
    assert "👍 Берём 1 · 👎 Мимо 0 · ✅ Уже в работе 0" in text
    assert "[4/5] <a href=\"https://example.com/a1\">Пожар на складе</a> · источников: 2" in text
