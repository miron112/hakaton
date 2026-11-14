"""Правила уведомлений, формат сообщения, лимит на цикл, обратная связь."""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import Engine, select

from radar.bot.handlers import parse_callback
from radar.bot.notifier import (
    EventView,
    format_digest,
    format_event_message,
    notify_pending,
    save_feedback,
)
from radar.config import get_settings
from radar.db import init_db, make_engine, session_scope
from radar.models import Analysis, Event, Feedback, Notification, RawItem, Source, utcnow
from radar.pipeline.dedup import assign_events
from radar.pipeline.notify_rules import decide
from tests.fakes import DedupClient, FakeSender, KeywordEmbedder
from tests.test_llm import VALID


def _analysis(**overrides) -> dict:
    return {**VALID, **overrides}


# --- правила ---

@pytest.mark.parametrize(
    ("overrides", "send", "reason"),
    [
        ({"geo_level": "khmao", "importance": 3}, True, "ok"),
        ({"geo_level": "khmao", "importance": 2}, False, "below_threshold"),
        ({"geo_level": "surgut_city", "importance": 2}, True, "ok"),  # для города порог ниже
        ({"geo_level": "surgut_city", "importance": 1}, False, "below_threshold"),
        ({"geo_level": "surgut_district", "importance": 2}, False, "below_threshold"),
        ({"is_relevant": False, "importance": 5}, False, "not_relevant"),
    ],
)
def test_send_rules(overrides: dict, send: bool, reason: str) -> None:
    decision = decide(_analysis(**overrides), get_settings())
    assert (decision.send, decision.reason) == (send, reason)


@pytest.mark.parametrize(
    ("urgency", "importance", "marker"),
    [("breaking", 5, "🔴"), ("breaking", 4, "🔴"), ("breaking", 3, "🟡"), ("today", 4, "🟠"), ("today", 3, "🟡")],
)
def test_markers(urgency: str, importance: int, marker: str) -> None:
    assert decide(_analysis(urgency=urgency, importance=importance), get_settings()).marker == marker


def test_local_only_option(monkeypatch) -> None:
    settings = get_settings()
    far = _analysis(geo_level="other", affects_surgut=False, importance=5)
    assert decide(far, settings).send is True  # по умолчанию — как в задании

    monkeypatch.setattr(settings, "notify_local_only", True)
    assert decide(far, settings).reason == "not_local"
    assert decide(_analysis(geo_level="other", affects_surgut=True, importance=5), settings).send


# --- формат ---

def _view(**overrides) -> EventView:
    return EventView(
        event_id=7,
        analysis=_analysis(**overrides),
        marker="🔴",
        source_name="Канал <Сургут>",
        source_role="source",
        url="https://t.me/chan/1?a=1&b=2",
        published_at=datetime(2026, 11, 14, 8, 30),
        first_seen_at=datetime(2026, 11, 14, 8, 41),
    )


def test_message_has_all_required_parts() -> None:
    text = format_event_message(_view())

    assert text.startswith("🔴 <b>[4/5] В 5 и 5А микрорайонах Сургута отключили холодную воду</b>")
    assert "Из-за аварии на сетях воды не будет до 18:00." in text
    assert "📍 Сургут, 5 микрорайон · #ЖКХ" in text
    assert '<a href="https://t.me/chan/1?a=1&amp;b=2">Канал &lt;Сургут&gt;</a>' in text
    assert "🕒 14.11 13:30 · найдено 13:41" in text  # время Сургута, UTC+5
    assert "требует проверки" not in text


def test_message_escapes_html_and_shows_verification() -> None:
    text = format_event_message(
        _view(
            headline="Цена <b>выросла</b> & упала", topic="транспорт и дороги", places=[],
            geo_level="khmao", needs_verification=True, verification_note="анонимный канал",
        )
    )
    assert "Цена &lt;b&gt;выросла&lt;/b&gt; &amp; упала" in text
    assert "📍 Югра · #транспорт_и_дороги" in text
    assert "⚠️ требует проверки: анонимный канал" in text


def test_digest_is_split_by_telegram_limit() -> None:
    views = [_view(headline="Очень длинный заголовок события " * 3) for _ in range(60)]
    chunks = format_digest(views)
    assert len(chunks) > 1 and all(len(chunk) <= 4096 for chunk in chunks)
    assert "Ещё 60 событий" in chunks[0]


# --- отправка ---

@pytest.fixture
def engine(tmp_path, monkeypatch) -> Engine:
    monkeypatch.setattr("radar.bot.notifier.SEND_PAUSE_SECONDS", 0)
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    with session_scope(engine) as session:
        session.add(Source(name="Канал", type="telegram", url="chan", geo="surgut_city"))
        session.add(Source(name="Своя лента", type="rss", url="https://example.com/own", geo="surgut_city", role="own"))
    return engine


def _add_analyzed(engine: Engine, key: str, source_id: int = 1, **overrides) -> None:
    with session_scope(engine) as session:
        item = RawItem(
            source_id=source_id, external_id=key, url=f"https://example.com/{key}", text="текст",
            published_at=utcnow(), content_hash=key, status="analyzed",
        )
        session.add(item)
        session.add(
            Analysis(
                raw_item=item, result=_analysis(headline=f"Событие {key}", **overrides),
                model="m", prompt_version="v",
            )
        )


def _assign(engine: Engine) -> None:
    # У каждого события свой ключ, поэтому похожих нет и LLM не вызывается
    embedder = KeywordEmbedder("важное", "мелочь", "своё", "срочное", "обычное-1", "обычное-2", "первое", "второе")
    asyncio.run(assign_events(embedder, DedupClient(), engine))


def _notify(engine: Engine, sender: FakeSender):
    _assign(engine)
    return asyncio.run(notify_pending(sender, engine))


def test_end_to_end_only_worthy_events_are_sent_once(engine: Engine) -> None:
    _add_analyzed(engine, "важное", importance=4)
    _add_analyzed(engine, "мелочь", geo_level="khmao", importance=2)
    _add_analyzed(engine, "шум", is_relevant=False, importance=1)
    _add_analyzed(engine, "своё", source_id=2, importance=5)
    sender = FakeSender()

    stats = _notify(engine, sender)

    assert (stats.sent, stats.in_digest, stats.failed) == (1, 0, 0)
    assert stats.skipped == {"below_threshold": 1, "own_source": 1}
    assert len(sender.events) == 1 and "Событие важное" in sender.events[0][1]
    with session_scope(engine) as session:
        assert len(session.scalars(select(Event)).all()) == 3  # нерелевантное событием не стало
        notification = session.scalar(select(Notification))
        assert (notification.type, notification.telegram_message_id) == ("new", 101)
        assert session.get(Event, notification.event_id).telegram_message_id == 101

    # Повторный цикл ничего не шлёт
    assert _notify(engine, sender).sent == 0 and len(sender.events) == 1


def test_flood_limit_sends_top_events_and_one_digest(engine: Engine, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_notifications_per_cycle", 2)
    _add_analyzed(engine, "обычное-1", importance=3, urgency="today")
    _add_analyzed(engine, "срочное", importance=5, urgency="breaking")
    _add_analyzed(engine, "обычное-2", importance=3, urgency="today")
    _add_analyzed(engine, "важное", importance=4, urgency="today")
    sender = FakeSender()

    stats = _notify(engine, sender)

    assert (stats.sent, stats.in_digest) == (2, 2)
    assert "Событие срочное" in sender.events[0][1] and "Событие важное" in sender.events[1][1]
    assert len(sender.texts) == 1 and "обычное-1" in sender.texts[0][0] and "обычное-2" in sender.texts[0][0]
    assert _notify(engine, sender).in_digest == 0  # события из сводки считаются отправленными


def test_failed_send_is_retried_next_cycle(engine: Engine) -> None:
    _add_analyzed(engine, "первое")
    _add_analyzed(engine, "второе")

    stats = _notify(engine, FakeSender(fail_on="первое"))
    assert (stats.sent, stats.failed) == (1, 1)

    retry = FakeSender()
    assert _notify(engine, retry).sent == 1 and "Событие первое" in retry.events[0][1]


# --- обратная связь ---

def test_feedback_is_saved_and_shown_in_message(engine: Engine) -> None:
    _add_analyzed(engine, "1")
    _assign(engine)

    text = save_feedback(1, user_id=555, user_name="Анна <ред>", action="take", engine=engine)

    assert text is not None and "Событие 1" in text
    assert "— 👍 Берём · Анна &lt;ред&gt; ·" in text
    with session_scope(engine) as session:
        feedback = session.scalar(select(Feedback))
        assert (feedback.event_id, feedback.user_id, feedback.action) == (1, 555, "take")
    assert save_feedback(999, 555, "Анна", "skip", engine=engine) is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("fb:take:12", ("take", 12)),
        ("fb:in_progress:3", ("in_progress", 3)),
        ("fb:delete:3", None),
        ("fb:take:abc", None),
        ("other:take:1", None),
        (None, None),
    ],
)
def test_callback_parsing(data, expected) -> None:
    assert parse_callback(data) == expected
