"""Метрики eval и выборки админки."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from radar.admin import queries
from radar.db import init_db, make_engine, session_scope
from radar.eval import Outcome, Row, compute_metrics, evaluate, load_rows
from radar.llm.base import LLMClient, LLMReply, Message
from radar.models import Analysis, Event, EventItem, Feedback, Notification, RawItem, Source, utcnow
from tests.test_llm import VALID


# --- eval ---

def _row(number: int, relevant: bool, importance: int | None = None, text: str = "текст") -> Row:
    return Row(number, text, "", "набор", "telegram", "нет", relevant, importance)


def test_metrics() -> None:
    outcomes = [
        Outcome(_row(2, True, 4), relevant=True, importance=5),    # верно показано, ошибка 1
        Outcome(_row(3, True, 3), relevant=False, importance=1),   # пропущено, ошибка 2
        Outcome(_row(4, False, 1), relevant=True, importance=3),   # лишнее, ошибка 2
        Outcome(_row(5, False), relevant=False, importance=1),     # верно отсеяно, без разметки важности
        Outcome(_row(6, True, 5), error="нет ответа"),             # не учитывается
    ]
    metrics = compute_metrics(outcomes)

    assert (metrics.total, metrics.answered) == (5, 4)
    assert (metrics.true_positive, metrics.false_positive, metrics.false_negative, metrics.true_negative) == (1, 1, 1, 1)
    assert metrics.precision == 0.5 and metrics.recall == 0.5
    assert metrics.importance_pairs == 3 and metrics.importance_mae == pytest.approx(5 / 3)


def test_metrics_without_positives_are_undefined() -> None:
    metrics = compute_metrics([Outcome(_row(2, False), relevant=False, importance=1)])
    assert metrics.precision is None and metrics.recall is None and metrics.importance_mae is None


def test_csv_loading(tmp_path: Path) -> None:
    path = tmp_path / "set.csv"
    path.write_text(
        "text;is_relevant;importance;title\n"
        '"Авария; воды нет";да;4;Авария\n'
        "Поздравление с юбилеем;0;;\n"
        ";1;3;пустой текст пропускается\n",
        encoding="utf-8-sig",
    )
    rows = load_rows(path)

    assert [(row.text, row.expected_relevant, row.expected_importance) for row in rows] == [
        ("Авария; воды нет", True, 4), ("Поздравление с юбилеем", False, None),
    ]
    assert len(load_rows("calibration.example.csv")) == 3

    path.write_text("text,is_relevant\nтекст,наверное\n", encoding="utf-8")
    with pytest.raises(ValueError, match="строка 2"):
        load_rows(path)
    path.write_text("body,label\nтекст,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is_relevant, text"):
        load_rows(path)


def test_evaluate_runs_prompt_and_survives_failures() -> None:
    class Client(LLMClient):
        model = "test"

        async def _chat(self, messages: list[Message]) -> LLMReply:
            if "СБОЙ" in messages[1]["content"]:
                raise ConnectionError("нет связи")
            relevant = "авария" in messages[1]["content"]
            return LLMReply(json.dumps({**VALID, "is_relevant": relevant}), prompt_tokens=10, completion_tokens=5)

    rows = [_row(2, True, 4, "авария на сетях"), _row(3, False, 1, "гороскоп"), _row(4, True, 3, "СБОЙ")]
    outcomes, usage = asyncio.run(evaluate(rows, Client()))

    assert [outcome.relevant for outcome in outcomes] == [True, False, None]
    assert "нет связи" in outcomes[2].error and usage.requests == 2
    assert compute_metrics(outcomes).precision == 1.0


# --- админка ---

@pytest.fixture
def engine(tmp_path) -> Engine:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    now = utcnow()
    with session_scope(engine) as session:
        channel = Source(name="Канал", type="telegram", url="chan", geo="surgut_city", priority=1,
                         last_success_at=now)
        portal = Source(name="Портал", type="rss", url="https://example.com/rss", geo="khmao",
                        last_error="HTTP 500", error_count=3)
        own = Source(name="Своя лента", type="rss", url="https://example.com/own", geo="surgut_city", role="own")
        session.add_all([channel, portal, own])

        def item(source: Source, key: str, relevant: bool, minutes_ago: int) -> RawItem:
            row = RawItem(source=source, external_id=key, url=f"https://example.com/{key}", text="т",
                          published_at=now - timedelta(minutes=minutes_ago), content_hash=key, status="analyzed")
            session.add(Analysis(raw_item=row, result={**VALID, "is_relevant": relevant}, model="m", prompt_version="v"))
            return row

        first = item(channel, "c1", True, 120)
        noise = item(channel, "c2", False, 100)
        repeat = item(portal, "p1", True, 90)
        own_item = item(own, "o1", True, 30)
        other = item(portal, "p2", True, 60)
        session.add(noise)

        fire = Event(headline="Пожар на складе", summary="Суть", key_facts=["факт"], geo_level="surgut_city",
                     topic="ЧП и происшествия", max_importance=5, first_seen_at=now - timedelta(minutes=110))
        road = Event(headline="Ремонт дороги", summary="Суть", key_facts=[], geo_level="khmao",
                     topic="транспорт и дороги", max_importance=2, first_seen_at=now - timedelta(minutes=50))
        session.add_all([
            fire, road,
            EventItem(event=fire, raw_item=first, relation="origin"),
            EventItem(event=fire, raw_item=repeat, relation="duplicate"),
            EventItem(event=fire, raw_item=own_item, relation="duplicate"),
            EventItem(event=road, raw_item=other, relation="origin"),
            Notification(event=fire, type="new", telegram_message_id=1),
            Feedback(event=fire, user_id=1, action="skip", created_at=now - timedelta(minutes=100)),
            Feedback(event=fire, user_id=1, action="take", created_at=now - timedelta(minutes=90)),
        ])
    return engine


def test_sources_table_and_update(engine: Engine) -> None:
    with session_scope(engine) as session:
        rows = {row["name"]: row for row in queries.sources_table(session)}
        assert rows["Канал"]["status"] == "работает" and rows["Канал"]["items"] == 2
        assert rows["Портал"]["status"] == "ошибка" and rows["Портал"]["last_error"] == "HTTP 500"
        assert rows["Своя лента"]["status"] == "не опрашивался"

        assert queries.update_source(session, rows["Портал"]["id"], False, 9) is True
        assert queries.update_source(session, rows["Портал"]["id"], False, 3) is False
        portal = session.get(Source, rows["Портал"]["id"])
        assert (portal.enabled, portal.priority) == (False, 3)


def test_journal_shows_sources_reaction_and_lead_time(engine: Engine) -> None:
    with session_scope(engine) as session:
        rows = queries.events_journal(session)
        assert [row["headline"] for row in rows] == ["Ремонт дороги", "Пожар на складе"]
        fire = rows[1]
        assert fire["first_source"] == "Канал" and fire["sources_count"] == 3
        assert fire["notified"] is True and fire["reaction"] == "take"  # последняя реакция
        assert fire["lead_minutes"] == 80  # найдено за 110 мин, редакция написала за 30 мин до «сейчас»
        assert [link["relation"] for link in fire["links"]] == ["origin", "duplicate", "duplicate"]
        assert rows[0]["lead_minutes"] is None and rows[0]["reaction"] == queries.REACTION_NONE

        assert len(queries.events_journal(session, min_importance=3)) == 1
        assert len(queries.events_journal(session, geo=["khmao"])) == 1
        assert len(queries.events_journal(session, topics=["ЖКХ"])) == 0
        assert len(queries.events_journal(session, reactions=[queries.REACTION_NONE])) == 1
        assert len(queries.events_journal(session, since=utcnow() - timedelta(minutes=60))) == 1


def test_stats(engine: Engine) -> None:
    with session_scope(engine) as session:
        daily = queries.daily_stats(session)
        assert sum(day["events"] for day in daily) == 2
        assert sum(day["take"] for day in daily) == 1 and sum(day["skip"] for day in daily) == 1

        quality = {row["source"]: row for row in queries.source_quality(session)}
        assert quality["Канал"]["analyzed"] == 2 and quality["Канал"]["noise_share"] == 0.5
        assert quality["Канал"]["events"] == 1 and quality["Канал"]["take"] == 1  # учтена последняя реакция
        assert quality["Канал"]["skip"] == 0
        assert quality["Портал"]["events"] == 1 and quality["Портал"]["noise_share"] == 0.0
