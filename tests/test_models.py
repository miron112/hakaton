import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from radar.collectors.base import CollectedItem
from radar.models import Analysis, Event, EventItem, Feedback, Notification, RawItem, Source


def _source() -> Source:
    return Source(name="Тест", type="rss", url="https://example.com/rss.xml", geo="surgut_city")


def test_all_tables_are_created(session: Session) -> None:
    tables = set(inspect(session.get_bind()).get_table_names())
    assert tables == {
        "sources", "raw_items", "analyses", "events", "event_items", "feedback", "notifications",
    }


def test_full_chain_round_trip(session: Session) -> None:
    source = _source()
    item = RawItem(source=source, external_id="1", text="текст", content_hash="h1")
    event = Event(
        headline="Заголовок", summary="Суть", key_facts=["факт"], geo_level="surgut_city",
        topic="ЖКХ", max_importance=4, embedding=[0.1, 0.2],
    )
    session.add_all([
        source,
        item,
        Analysis(raw_item=item, result={"importance": 4}, model="m", prompt_version="v0.1"),
        event,
        EventItem(event=event, raw_item=item, relation="origin"),
        Feedback(event=event, user_id=5_000_000_000, action="take"),
        Notification(event=event, type="new", telegram_message_id=42),
    ])
    session.flush()
    session.expire_all()

    loaded = session.get(Event, event.id)
    assert loaded is not None
    assert loaded.embedding == [0.1, 0.2] and loaded.key_facts == ["факт"]
    assert loaded.items[0].raw_item.analysis.result == {"importance": 4}
    assert loaded.feedback[0].user_id == 5_000_000_000
    assert item.status == "new" and item.fetched_at is not None


def test_content_hash_is_unique(session: Session) -> None:
    source = _source()
    session.add_all([
        source,
        RawItem(source=source, external_id="1", text="a", content_hash="same"),
        RawItem(source=source, external_id="2", text="a", content_hash="same"),
    ])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_content_hash_ignores_case_and_whitespace() -> None:
    first = CollectedItem("1", None, "Заголовок", "Текст  новости\n", None)
    second = CollectedItem("2", None, "заголовок", "текст новости", None)
    assert first.content_hash == second.content_hash
