"""Выборки для админки. Отдельно от интерфейса, чтобы их можно было тестировать."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from radar.models import Analysis, Event, EventItem, Feedback, Notification, RawItem, Source, utcnow
from radar.timeutil import LOCAL_TZ, format_local

REACTION_NONE = "без реакции"


def local_day(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def sources_table(session: Session) -> list[dict[str, Any]]:
    counts = dict(
        session.execute(select(RawItem.source_id, func.count()).group_by(RawItem.source_id)).all()
    )
    return [
        {
            "id": source.id,
            "enabled": source.enabled,
            "priority": source.priority,
            "name": source.name,
            "type": source.type,
            "geo": source.geo,
            "role": source.role,
            "address": source.url,
            "status": (
                "ошибка" if source.last_error
                else "работает" if source.last_success_at
                else "не опрашивался"
            ),
            "last_success": format_local(source.last_success_at) if source.last_success_at else "",
            "errors_in_row": source.error_count,
            "last_error": source.last_error or "",
            "items": counts.get(source.id, 0),
        }
        for source in session.scalars(select(Source).order_by(Source.priority, Source.name))
    ]


def update_source(session: Session, source_id: int, enabled: bool, priority: int) -> bool:
    """Возвращает True, если что-то изменилось."""
    source = session.get_one(Source, source_id)
    priority = min(3, max(1, int(priority)))
    if (source.enabled, source.priority) == (bool(enabled), priority):
        return False
    source.enabled, source.priority = bool(enabled), priority
    return True


def events_journal(
    session: Session,
    since: datetime | None = None,
    until: datetime | None = None,
    geo: list[str] | None = None,
    topics: list[str] | None = None,
    min_importance: int = 1,
    reactions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Журнал находок: когда радар нашёл событие и когда о нём написала сама редакция."""
    query = select(Event).where(Event.max_importance >= min_importance)
    if since:
        query = query.where(Event.first_seen_at >= since)
    if until:
        query = query.where(Event.first_seen_at < until)
    if geo:
        query = query.where(Event.geo_level.in_(geo))
    if topics:
        query = query.where(Event.topic.in_(topics))
    events = session.scalars(query.order_by(Event.first_seen_at.desc())).all()
    if not events:
        return []

    event_ids = [event.id for event in events]
    links_by_event: dict[int, list[EventItem]] = defaultdict(list)
    for link in session.scalars(
        select(EventItem)
        .options(joinedload(EventItem.raw_item).joinedload(RawItem.source))
        .where(EventItem.event_id.in_(event_ids))
        .order_by(EventItem.id)
    ):
        links_by_event[link.event_id].append(link)
    last_reaction: dict[int, str] = {}
    for feedback in session.scalars(
        select(Feedback).where(Feedback.event_id.in_(event_ids)).order_by(Feedback.created_at)
    ):
        last_reaction[feedback.event_id] = feedback.action
    notified = set(
        session.scalars(select(Notification.event_id).where(Notification.event_id.in_(event_ids)))
    )

    rows = []
    for event in events:
        reaction = last_reaction.get(event.id, REACTION_NONE)
        if reactions and reaction not in reactions:
            continue
        links = links_by_event[event.id]
        external = [link for link in links if link.raw_item.source.role != "own"]
        own_times = [
            link.raw_item.published_at
            for link in links
            if link.raw_item.source.role == "own" and link.raw_item.published_at
        ]
        own_published = min(own_times) if own_times else None
        # Опережение считаем только если событие пришло из внешнего источника
        lead = None
        if own_published and external:
            lead = round((own_published - event.first_seen_at).total_seconds() / 60)
        origin = next((link for link in links if link.relation == "origin"), None)
        rows.append(
            {
                "id": event.id,
                "found": format_local(event.first_seen_at),
                "importance": event.max_importance,
                "headline": event.headline,
                "geo": event.geo_level,
                "topic": event.topic,
                "first_source": origin.raw_item.source.name if origin else "",
                "published": format_local(origin.raw_item.published_at) if origin else "",
                "sources_count": len(links),
                "notified": event.id in notified,
                "reaction": reaction,
                "own_published": format_local(own_published) if own_published else "",
                "lead_minutes": lead,
                "summary": event.summary,
                "key_facts": list(event.key_facts or []),
                "links": [
                    {
                        "source": link.raw_item.source.name,
                        "relation": link.relation,
                        "url": link.raw_item.url or "",
                        "published": format_local(link.raw_item.published_at),
                    }
                    for link in links
                ],
            }
        )
    return rows


def daily_stats(session: Session, days: int = 14) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"events": 0, "notifications": 0, "take": 0, "skip": 0, "in_progress": 0}
    )
    for (seen,) in session.execute(select(Event.first_seen_at).where(Event.first_seen_at >= since)):
        stats[local_day(seen)]["events"] += 1
    for (sent,) in session.execute(select(Notification.sent_at).where(Notification.sent_at >= since)):
        stats[local_day(sent)]["notifications"] += 1
    for created, action in session.execute(
        select(Feedback.created_at, Feedback.action).where(Feedback.created_at >= since)
    ):
        stats[local_day(created)][action] += 1
    return [{"day": day, **values} for day, values in sorted(stats.items())]


def source_quality(session: Session) -> list[dict[str, Any]]:
    """Полезность источника: реакции на его события; шум — доля нерелевантных материалов."""
    rows: dict[int, dict[str, Any]] = {
        source.id: {
            "source": source.name, "role": source.role, "analyzed": 0, "relevant": 0,
            "events": 0, "take": 0, "in_progress": 0, "skip": 0,
        }
        for source in session.scalars(select(Source))
    }
    for source_id, result in session.execute(
        select(RawItem.source_id, Analysis.result).join(Analysis, Analysis.raw_item_id == RawItem.id)
    ):
        rows[source_id]["analyzed"] += 1
        rows[source_id]["relevant"] += 1 if result.get("is_relevant") else 0

    origin_source: dict[int, int] = {}
    for event_id, source_id in session.execute(
        select(EventItem.event_id, RawItem.source_id)
        .join(RawItem, RawItem.id == EventItem.raw_item_id)
        .where(EventItem.relation == "origin")
    ):
        origin_source[event_id] = source_id
        rows[source_id]["events"] += 1
    last_reaction: dict[int, str] = {}
    for event_id, action in session.execute(
        select(Feedback.event_id, Feedback.action).order_by(Feedback.created_at)
    ):
        last_reaction[event_id] = action
    for event_id, action in last_reaction.items():
        if event_id in origin_source:
            rows[origin_source[event_id]][action] += 1

    for row in rows.values():
        row["noise_share"] = (
            round(1 - row["relevant"] / row["analyzed"], 2) if row["analyzed"] else None
        )
    return sorted(rows.values(), key=lambda row: (-row["take"], -row["events"]))
