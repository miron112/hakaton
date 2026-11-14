"""Дедупликация: кандидаты по эмбеддингам, решение Промта 2, обновления ответом на сообщение."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import Engine, create_engine, inspect, select, text

from radar.bot.notifier import notify_pending
from radar.db import init_db, make_engine, session_scope
from radar.models import Analysis, Event, EventItem, Notification, RawItem, Source, utcnow
from radar.pipeline.dedup import DedupResult, assign_events
from radar.pipeline.embeddings import cosine
from tests.fakes import DedupClient, FakeSender, KeywordEmbedder
from tests.test_llm import VALID

EMBEDDER = KeywordEmbedder("пожар", "вода")


@pytest.fixture
def engine(tmp_path, monkeypatch) -> Engine:
    monkeypatch.setattr("radar.bot.notifier.SEND_PAUSE_SECONDS", 0)
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        session.add(Source(name="Канал", type="telegram", url="chan", geo="surgut_city"))
        session.add(Source(name="Портал", type="rss", url="https://example.com/rss", geo="khmao"))
        session.add(Source(name="Своя лента", type="rss", url="https://example.com/own", geo="surgut_city", role="own"))
    return engine


def _add(engine: Engine, key: str, headline: str, source_id: int = 1, **overrides) -> None:
    with session_scope(engine) as session:
        item = RawItem(
            source_id=source_id, external_id=key, url=f"https://example.com/{key}", text="текст",
            published_at=utcnow(), content_hash=key, status="analyzed",
        )
        session.add(item)
        session.add(
            Analysis(
                raw_item=item, model="m", prompt_version="v",
                result={**VALID, "headline": headline, "key_facts": ["исходный факт"], **overrides},
            )
        )


def _assign(engine: Engine, client: DedupClient):
    return asyncio.run(assign_events(EMBEDDER, client, engine))


def _links(engine: Engine) -> list[tuple[int, str]]:
    with session_scope(engine) as session:
        return [(link.event_id, link.relation) for link in session.scalars(select(EventItem).order_by(EventItem.id))]


def test_no_candidates_means_new_event_without_llm(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "Отключили вода в 5 микрорайоне")
    client = DedupClient()

    stats = _assign(engine, client)

    assert (stats.new_events, stats.duplicates, stats.updates) == (2, 0, 0)
    assert client.prompts == [] and stats.usage.requests == 0
    with session_scope(engine) as session:
        assert all(event.embedding for event in session.scalars(select(Event)))


def test_duplicate_is_attached_and_not_sent_again(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "На складе пожар", source_id=2)
    client = DedupClient({"relation": "duplicate", "event_id": "1", "new_facts": [], "reason": "то же"})
    sender = FakeSender()

    stats = _assign(engine, client)
    notified = asyncio.run(notify_pending(sender, engine))

    assert (stats.new_events, stats.duplicates) == (1, 1)
    assert _links(engine) == [(1, "origin"), (1, "duplicate")]
    assert (notified.sent, notified.updates) == (1, 0) and len(sender.events) == 1
    # В Промт 2 ушли новый материал и известное событие
    assert "Заголовок: На складе пожар" in client.prompts[0]
    assert "[1]" in client.prompts[0] and "Пожар: горит склад" in client.prompts[0]


def test_update_is_sent_as_reply_to_original_message(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад", importance=3)
    sender = FakeSender()
    _assign(engine, DedupClient())
    asyncio.run(notify_pending(sender, engine))
    original_message_id = 101

    _add(engine, "2", "Пожар на складе: есть пострадавшие", source_id=2, importance=5)
    client = DedupClient({
        "relation": "update", "event_id": 1, "new_facts": ["Пострадали два человека"],
        "is_official_confirmation": True, "reason": "новые факты",
    })
    stats = _assign(engine, client)
    notified = asyncio.run(notify_pending(sender, engine))

    assert stats.updates == 1 and (notified.sent, notified.updates) == (0, 1)
    update_text, reply_to = sender.texts[0]
    assert reply_to == original_message_id
    assert "Обновление: Пожар: горит склад" in update_text
    assert "• Официальное подтверждение" in update_text and "• Пострадали два человека" in update_text
    assert "Портал" in update_text
    with session_scope(engine) as session:
        event = session.get(Event, 1)
        assert event.max_importance == 5
        assert event.key_facts == ["исходный факт", "Официальное подтверждение", "Пострадали два человека"]
        update = session.scalar(select(Notification).where(Notification.type == "update"))
        assert update.raw_item_id == 2

    # Повторный цикл не шлёт обновление второй раз
    assert asyncio.run(notify_pending(sender, engine)).updates == 0 and len(sender.texts) == 1


def test_update_without_really_new_facts_becomes_duplicate(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "На складе пожар", source_id=2)
    # Модель назвала это обновлением, но её «новый факт» уже есть в событии
    client = DedupClient({"relation": "update", "event_id": "1", "new_facts": ["исходный факт"]})

    stats = _assign(engine, client)

    assert (stats.updates, stats.duplicates) == (0, 1)
    assert _links(engine) == [(1, "origin"), (1, "duplicate")]


def test_update_keeps_only_new_facts_and_official_confirmation(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "На складе пожар", source_id=2)
    _add(engine, "3", "Пожар на складе подтвердили", source_id=2)
    client = DedupClient(
        {"relation": "update", "event_id": "1", "new_facts": ["исходный факт", "Эвакуированы 30 человек"]},
        {"relation": "update", "event_id": "1", "new_facts": [], "is_official_confirmation": True},
    )

    assert _assign(engine, client).updates == 2
    with session_scope(engine) as session:
        updates = session.scalars(
            select(EventItem).where(EventItem.relation == "update").order_by(EventItem.id)
        ).all()
        assert updates[0].new_facts == ["Эвакуированы 30 человек"]
        assert updates[1].new_facts == ["Официальное подтверждение"]


def test_similar_but_different_event_stays_separate(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "Пожар в жилом доме на другом конце города")
    client = DedupClient({"relation": "new_event", "event_id": None, "reason": "другой пожар"})

    assert _assign(engine, client).new_events == 2
    assert _links(engine) == [(1, "origin"), (2, "origin")]


def test_unknown_event_id_falls_back_to_best_candidate(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "На складе пожар")
    _assign(engine, DedupClient({"relation": "duplicate", "event_id": "999"}))
    assert _links(engine) == [(1, "origin"), (1, "duplicate")]


def test_invalid_answer_creates_new_event_and_api_error_retries_later(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _add(engine, "2", "На складе пожар")
    stats = _assign(engine, DedupClient("не JSON", "снова не JSON"))
    assert stats.new_events == 2  # лучше лишнее уведомление, чем пропуск

    _add(engine, "3", "Пожар потушен")
    stats = _assign(engine, DedupClient(ConnectionError("нет связи")))
    assert stats.failed == 1 and len(_links(engine)) == 2

    stats = _assign(engine, DedupClient({"relation": "duplicate", "event_id": "1"}))
    assert stats.duplicates == 1 and len(_links(engine)) == 3


def test_old_events_are_not_candidates(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    _assign(engine, DedupClient())
    with session_scope(engine) as session:
        session.get(Event, 1).last_updated_at = utcnow() - timedelta(hours=100)

    _add(engine, "2", "Пожар: горит склад снова")
    client = DedupClient()
    assert _assign(engine, client).new_events == 1 and client.prompts == []


def test_irrelevant_items_do_not_become_events(engine: Engine) -> None:
    _add(engine, "1", "Пожар в другом регионе", is_relevant=False)
    assert _assign(engine, DedupClient()).new_events == 0 and _links(engine) == []


def test_own_publication_is_linked_but_never_sent(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад")
    sender = FakeSender()
    _assign(engine, DedupClient())
    asyncio.run(notify_pending(sender, engine))

    _add(engine, "2", "Пожар на складе: подробности", source_id=3)
    _assign(engine, DedupClient({"relation": "update", "event_id": "1", "new_facts": ["Площадь 200 м²"]}))
    notified = asyncio.run(notify_pending(sender, engine))

    assert _links(engine) == [(1, "origin"), (1, "update")]  # в журнале видно, что редакция написала
    assert notified.updates == 0 and sender.texts == []


def test_update_of_event_that_was_not_shown_is_not_sent(engine: Engine) -> None:
    _add(engine, "1", "Пожар: горит склад", geo_level="khmao", importance=1)
    _add(engine, "2", "Пожар на складе: уточнение", geo_level="khmao", importance=1)
    _assign(engine, DedupClient({"relation": "update", "event_id": "1", "new_facts": ["Уточнение"]}))
    sender = FakeSender()

    notified = asyncio.run(notify_pending(sender, engine))

    assert (notified.sent, notified.updates) == (0, 0) and sender.texts == []


def test_event_id_is_normalized() -> None:
    assert DedupResult(relation="duplicate", event_id=12).event_id == "12"
    assert DedupResult(relation="duplicate", event_id="[12]").event_id == "12"
    assert cosine([1.0, 0.0], [0.6, 0.8]) == pytest.approx(0.6)


def test_init_db_adds_new_columns_to_existing_tables(tmp_path) -> None:
    path = tmp_path / "old.db"
    old = create_engine(f"sqlite:///{path}")
    with old.begin() as connection:
        connection.execute(text(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY, event_id INTEGER, sent_at DATETIME, "
            "type VARCHAR(16), telegram_message_id BIGINT)"
        ))
    old.dispose()

    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("notifications")}
    assert "raw_item_id" in columns
