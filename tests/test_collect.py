"""Цикл сбора с подменёнными сборщиками: новизна, первый запуск, изоляция ошибок."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from radar.collectors.base import CollectedItem, Collector
from radar.config import get_settings
from radar.db import init_db, make_engine, session_scope
from radar.models import RawItem, Source, utcnow
from radar.pipeline import collect


def _item(key: str, age_hours: float = 1, text: str | None = None) -> CollectedItem:
    return CollectedItem(
        external_id=key,
        url=f"https://example.com/{key}",
        title=f"Заголовок {key}",
        text=text or f"Текст материала {key}",
        published_at=utcnow() - timedelta(hours=age_hours),
    )


class FakeCollector(Collector):
    feeds: dict[str, list[CollectedItem] | Exception] = {}
    calls: dict[str, int] = {}

    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        self.calls[source.url] = self.calls.get(source.url, 0) + 1
        feed = self.feeds[source.url]
        if isinstance(feed, Exception):
            raise feed
        return feed


@pytest.fixture
def engine(tmp_path, monkeypatch) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    FakeCollector.feeds, FakeCollector.calls = {}, {}
    monkeypatch.setattr(collect, "COLLECTORS", {"rss": FakeCollector})
    monkeypatch.setattr(get_settings(), "fetch_retry_pause_seconds", 0)
    return engine


def _add_source(engine: Engine, url: str, **fields) -> None:
    with session_scope(engine) as session:
        session.add(Source(name=url, type=fields.pop("type", "rss"), url=url, geo="khmao", **fields))


def _run(engine: Engine) -> dict[str, collect.SourceResult]:
    return {result.source_name: result for result in asyncio.run(collect.collect_all(engine))}


def _statuses(engine: Engine) -> dict[str, str]:
    with session_scope(engine) as session:
        return {row.external_id: row.status for row in session.scalars(select(RawItem))}


def test_same_post_is_not_saved_twice(engine: Engine) -> None:
    _add_source(engine, "a")
    FakeCollector.feeds["a"] = [_item("1"), _item("2")]

    assert _run(engine)["a"].new == 2
    FakeCollector.feeds["a"] = [_item("1"), _item("2"), _item("3")]
    second = _run(engine)["a"]

    assert (second.new, second.duplicates) == (1, 2)
    assert len(_statuses(engine)) == 3


def test_same_text_from_another_source_is_a_duplicate(engine: Engine) -> None:
    _add_source(engine, "a")
    _add_source(engine, "b")
    FakeCollector.feeds["a"] = [CollectedItem("a1", None, "Заголовок", "Один текст", utcnow())]
    FakeCollector.feeds["b"] = [CollectedItem("b1", None, "ЗАГОЛОВОК", "один  текст", utcnow())]

    results = _run(engine)

    assert results["a"].new + results["b"].new == 1
    assert results["a"].duplicates + results["b"].duplicates == 1


def test_first_run_does_not_process_history(engine: Engine) -> None:
    _add_source(engine, "a")
    FakeCollector.feeds["a"] = [_item("fresh", age_hours=2), _item("old", age_hours=100)]

    first = _run(engine)["a"]
    assert (first.new, first.old) == (1, 1)
    assert _statuses(engine) == {"fresh": "new", "old": "filtered_out"}

    # На втором цикле старый пост не должен всплыть как новый
    second = _run(engine)["a"]
    assert (second.new, second.duplicates) == (0, 2)


def test_failing_source_does_not_stop_others(engine: Engine) -> None:
    _add_source(engine, "bad")
    _add_source(engine, "good")
    FakeCollector.feeds["bad"] = RuntimeError("сайт лежит")
    FakeCollector.feeds["good"] = [_item("1")]

    results = _run(engine)

    assert results["good"].new == 1
    assert "сайт лежит" in (results["bad"].error or "")
    assert FakeCollector.calls["bad"] == get_settings().fetch_retries + 1
    with session_scope(engine) as session:
        bad = session.scalar(select(Source).where(Source.url == "bad"))
        good = session.scalar(select(Source).where(Source.url == "good"))
        assert bad.error_count == 1 and "сайт лежит" in bad.last_error
        assert bad.last_success_at is None
        assert good.error_count == 0 and good.last_success_at is not None


def test_error_is_cleared_after_recovery(engine: Engine) -> None:
    _add_source(engine, "a")
    FakeCollector.feeds["a"] = RuntimeError("временно")
    _run(engine)
    FakeCollector.feeds["a"] = [_item("1")]
    _run(engine)

    with session_scope(engine) as session:
        source = session.scalar(select(Source))
        assert source.last_error is None and source.error_count == 0


def test_disabled_and_unsupported_sources_are_not_fetched(engine: Engine) -> None:
    _add_source(engine, "off", enabled=False)
    _add_source(engine, "site", type="vk")
    FakeCollector.feeds["off"] = [_item("1")]

    results = _run(engine)

    assert "off" not in results
    assert results["site"].skipped_reason and results["site"].error is None
    assert FakeCollector.calls == {}
