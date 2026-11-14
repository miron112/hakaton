"""Формирование и отправка уведомлений редактору."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from typing import Any, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from radar.config import Settings, get_settings
from radar.db import session_scope
from radar.log import log_event
from radar.models import Event, EventItem, Feedback, Notification, utcnow
from radar.pipeline.notify_rules import MARK_BREAKING, decide
from radar.timeutil import format_local

ACTION_LABELS = {"take": "👍 Берём", "skip": "👎 Мимо", "in_progress": "✅ Уже в работе"}
GEO_LABELS = {
    "surgut_city": "Сургут",
    "surgut_district": "Сургутский район",
    "khmao": "Югра",
    "other": "вне Югры",
    "unknown": "место не определено",
}
TELEGRAM_TEXT_LIMIT = 3800  # с запасом от лимита 4096
SEND_PAUSE_SECONDS = 0.3


class Sender(Protocol):
    async def send_event(self, text: str, event_id: int, reply_to: int | None = None) -> int: ...

    async def send_text(self, text: str, reply_to: int | None = None) -> int: ...


@dataclass
class UpdateView:
    event_id: int
    raw_item_id: int
    headline: str
    new_facts: list[str]
    source_name: str
    url: str | None
    published_at: datetime | None
    reply_to: int


@dataclass
class EventView:
    """Всё, что нужно для текста уведомления."""

    event_id: int
    analysis: dict[str, Any]
    marker: str
    source_name: str
    source_role: str
    url: str | None
    published_at: datetime | None
    first_seen_at: datetime


@dataclass
class NotifyStats:
    sent: int = 0
    updates: int = 0
    in_digest: int = 0
    failed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


def load_event_view(session: Session, event: Event, settings: Settings) -> EventView | None:
    origin = session.scalar(
        select(EventItem).where(EventItem.event_id == event.id, EventItem.relation == "origin")
    )
    if origin is None or origin.raw_item.analysis is None:
        return None
    item = origin.raw_item
    # Обновления могли поднять важность события выше оценки первого материала
    analysis = {**item.analysis.result}
    analysis["importance"] = max(analysis["importance"], event.max_importance)
    return EventView(
        event_id=event.id,
        analysis=analysis,
        marker=decide(analysis, settings).marker,
        source_name=item.source.name,
        source_role=item.source.role,
        url=item.url,
        published_at=item.published_at,
        first_seen_at=event.first_seen_at,
    )


def _hashtag(topic: str) -> str:
    return "#" + "_".join(topic.replace("-", " ").split())


def format_event_message(view: EventView) -> str:
    """Текст уведомления в HTML-разметке Telegram."""
    analysis = view.analysis
    places = ", ".join(analysis.get("places", [])[:3]) or GEO_LABELS[analysis["geo_level"]]
    source = escape(view.source_name)
    if view.url:
        source = f'<a href="{escape(view.url, quote=True)}">{source}</a>'
    time_line = (
        f"🕒 {format_local(view.published_at, '%d.%m %H:%M')}"
        f" · найдено {format_local(view.first_seen_at, '%H:%M')}"
    )
    lines = [
        f"{view.marker} <b>[{analysis['importance']}/5] {escape(analysis['headline'])}</b>",
        escape(analysis["summary"]),
        f"📍 {escape(places)} · {escape(_hashtag(analysis['topic']))}",
        f"🔗 {source}",
        time_line,
    ]
    if analysis.get("needs_verification"):
        note = analysis.get("verification_note")
        lines.append("⚠️ требует проверки" + (f": {escape(note)}" if note else ""))
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT]


def format_update_message(view: UpdateView) -> str:
    source = escape(view.source_name)
    if view.url:
        source = f'<a href="{escape(view.url, quote=True)}">{source}</a>'
    facts = [f"• {escape(fact)}" for fact in view.new_facts] or ["• появились новые подробности"]
    lines = [
        f"🔄 <b>Обновление: {escape(view.headline)}</b>",
        *facts,
        f"🔗 {source}",
        f"🕒 {format_local(view.published_at, '%d.%m %H:%M')}",
    ]
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT]


def format_feedback_line(action: str, user_name: str, at: datetime) -> str:
    return f"— {ACTION_LABELS[action]} · {escape(user_name)} · {format_local(at, '%H:%M')}"


def format_digest(views: list[EventView]) -> list[str]:
    """Сводка для событий сверх лимита цикла; режется на части по лимиту Telegram."""
    header = f"📋 <b>Ещё {len(views)} событий за цикл</b> (сверх лимита уведомлений)"
    chunks, current = [], header
    for view in views:
        title = escape(view.analysis["headline"])
        if view.url:
            title = f'<a href="{escape(view.url, quote=True)}">{title}</a>'
        line = f"{view.marker} [{view.analysis['importance']}/5] {title} — {escape(view.source_name)}"
        if len(current) + len(line) + 1 > TELEGRAM_TEXT_LIMIT:
            chunks.append(current)
            current = "📋 <b>Продолжение сводки</b>"
        current += "\n" + line
    return chunks + [current]


def _pending(session: Session, settings: Settings, stats: NotifyStats) -> list[EventView]:
    """События без уведомления, которые по правилам надо показать редактору."""
    notified = select(Notification.event_id).where(Notification.type == "new")
    events = session.scalars(
        select(Event).where(
            Event.id.not_in(notified),
            Event.first_seen_at >= utcnow() - timedelta(hours=settings.max_age_hours),
        )
    ).all()
    views: list[EventView] = []
    for event in events:
        view = load_event_view(session, event, settings)
        if view is None:
            continue
        reason = "own_source" if view.source_role == "own" else decide(view.analysis, settings).reason
        if reason != "ok":
            stats.skipped[reason] = stats.skipped.get(reason, 0) + 1
            continue
        views.append(view)
    # Сначала срочное, затем по важности, затем более свежее
    views.sort(
        key=lambda view: (
            view.marker != MARK_BREAKING,
            -view.analysis["importance"],
            -view.first_seen_at.timestamp(),
        )
    )
    return views


def _pending_updates(session: Session, settings: Settings) -> list[UpdateView]:
    """Обновления уже показанных редактору событий, о которых ещё не сообщали."""
    sent = select(Notification.raw_item_id).where(Notification.raw_item_id.is_not(None))
    links = session.scalars(
        select(EventItem)
        .join(Event)
        .where(
            EventItem.relation == "update",
            EventItem.raw_item_id.not_in(sent),
            Event.telegram_message_id.is_not(None),
            Event.last_updated_at >= utcnow() - timedelta(hours=settings.max_age_hours),
        )
        .order_by(EventItem.id)
    ).all()
    return [
        UpdateView(
            event_id=link.event_id,
            raw_item_id=link.raw_item_id,
            headline=link.event.headline,
            new_facts=link.new_facts or [],
            source_name=link.raw_item.source.name,
            url=link.raw_item.url,
            published_at=link.raw_item.published_at,
            reply_to=link.event.telegram_message_id,
        )
        for link in links
        if link.raw_item.source.role != "own"
    ]


async def _send_updates(sender: Sender, engine: Engine | None, settings: Settings, stats: NotifyStats) -> None:
    with session_scope(engine) as session:
        updates = _pending_updates(session, settings)
    # Сверх лимита — в следующем цикле: обновления хранятся в базе и не теряются
    for view in updates[: settings.max_notifications_per_cycle]:
        try:
            message_id = await sender.send_text(format_update_message(view), reply_to=view.reply_to)
        except Exception as error:
            stats.failed += 1
            log_event(
                "notify", "update_error", view.source_name, logging.ERROR,
                event=view.event_id, error=f"{type(error).__name__}: {error}"[:300],
            )
            continue
        with session_scope(engine) as session:
            session.add(
                Notification(
                    event_id=view.event_id, type="update", telegram_message_id=message_id,
                    raw_item_id=view.raw_item_id,
                )
            )
        stats.updates += 1
        log_event("notify", "update_sent", view.source_name, event=view.event_id, message=message_id)
        await asyncio.sleep(SEND_PAUSE_SECONDS)


def _record(engine: Engine | None, event_ids: list[int], message_id: int) -> None:
    with session_scope(engine) as session:
        for event_id in event_ids:
            session.add(Notification(event_id=event_id, type="new", telegram_message_id=message_id))
            session.get_one(Event, event_id).telegram_message_id = message_id


async def notify_pending(sender: Sender, engine: Engine | None = None) -> NotifyStats:
    """Отправляет уведомления о новых событиях: до лимита — отдельно, остальное — сводкой."""
    settings = get_settings()
    stats = NotifyStats()
    with session_scope(engine) as session:
        views = _pending(session, settings, stats)

    limit = settings.max_notifications_per_cycle
    for view in views[:limit]:
        try:
            message_id = await sender.send_event(format_event_message(view), view.event_id)
        except Exception as error:  # не отправилось — попробуем в следующем цикле
            stats.failed += 1
            log_event(
                "notify", "error", view.source_name, logging.ERROR,
                event=view.event_id, error=f"{type(error).__name__}: {error}"[:300],
            )
            continue
        _record(engine, [view.event_id], message_id)
        stats.sent += 1
        log_event(
            "notify", "sent", view.source_name, event=view.event_id,
            importance=view.analysis["importance"], message=message_id,
        )
        await asyncio.sleep(SEND_PAUSE_SECONDS)

    rest = views[limit:]
    if rest:
        try:
            message_id = 0
            for chunk in format_digest(rest):
                message_id = await sender.send_text(chunk)
            _record(engine, [view.event_id for view in rest], message_id)
            stats.in_digest = len(rest)
            log_event("notify", "digest", events=len(rest), message=message_id)
        except Exception as error:
            stats.failed += len(rest)
            log_event(
                "notify", "digest_error", level=logging.ERROR,
                error=f"{type(error).__name__}: {error}"[:300],
            )

    # После новых: событие и его обновление могли прийти в одном цикле
    await _send_updates(sender, engine, settings, stats)
    return stats


def save_feedback(
    event_id: int, user_id: int, user_name: str, action: str, engine: Engine | None = None
) -> str | None:
    """Сохраняет реакцию и возвращает новый текст сообщения (None — событие не найдено)."""
    settings = get_settings()
    with session_scope(engine) as session:
        event = session.get(Event, event_id)
        view = load_event_view(session, event, settings) if event else None
        if view is None:
            return None
        feedback = Feedback(event_id=event_id, user_id=user_id, action=action, created_at=utcnow())
        session.add(feedback)
        log_event("feedback", action, view.source_name, event=event_id, user=user_id)
        return (
            format_event_message(view)
            + "\n\n"
            + format_feedback_line(action, user_name, feedback.created_at)
        )
