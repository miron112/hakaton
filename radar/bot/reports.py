"""Тексты для команд /status и /today."""

from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from radar.bot.notifier import ACTION_LABELS
from radar.config import get_settings
from radar.db import session_scope
from radar.models import Analysis, Event, EventItem, Feedback, Notification, RawItem, Source, utcnow
from radar.pipeline.cycle import CycleState
from radar.pipeline.notify_rules import marker_for
from radar.timeutil import LOCAL_TZ, format_local

SILENT_AFTER_HOURS = 72
TOP_EVENTS = 10


def _local_day_start_utc(now: datetime | None = None) -> datetime:
    local_now = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _count(session: Session, column, *conditions) -> int:
    return session.scalar(select(func.count(column)).where(*conditions)) or 0


def _source_lines(session: Session) -> list[str]:
    sources = session.scalars(select(Source).order_by(Source.priority, Source.name)).all()
    enabled = [source for source in sources if source.enabled]
    failing = [source for source in enabled if source.last_error]
    never = [source for source in enabled if not source.last_error and source.last_success_at is None]
    alive = len(enabled) - len(failing) - len(never)

    lines = [f"Источники: работают {alive} из {len(enabled)}" + (
        f" (выключено {len(sources) - len(enabled)})" if len(sources) > len(enabled) else ""
    )]
    for source in failing:
        lines.append(
            f"❌ {escape(source.name)} — ошибок подряд: {source.error_count}; "
            f"{escape((source.last_error or '')[:150])}"
        )
    for source in never:
        lines.append(f"⏳ {escape(source.name)} — ещё не опрашивался")

    # Источник отвечает, но давно ничего не публикует — возможно, канал заброшен
    silent_before = utcnow() - timedelta(hours=SILENT_AFTER_HOURS)
    latest = dict(
        session.execute(
            select(RawItem.source_id, func.max(RawItem.published_at)).group_by(RawItem.source_id)
        ).all()
    )
    for source in enabled:
        last_post = latest.get(source.id)
        if source.last_success_at and not source.last_error and last_post and last_post < silent_before:
            lines.append(
                f"💤 {escape(source.name)} — последняя публикация {format_local(last_post, '%d.%m.%Y')}"
            )
    return lines


def _cycle_lines(state: CycleState, next_run_at: datetime | None) -> list[str]:
    report = state.last_report
    if report is None:
        lines = ["Последний цикл: ещё не было" + (" (идёт первый)" if state.running else "")]
    else:
        sent = 0
        if report.notify:
            sent = report.notify.sent + report.notify.updates + report.notify.in_digest
        lines = [
            f"Последний цикл: {format_local(report.started_at, '%d.%m %H:%M')}, "
            f"{report.duration_seconds} с" + (" · сейчас идёт следующий" if state.running else ""),
            f"  новых материалов {report.new_items}, "
            f"событий {report.dedup.new_events if report.dedup else 0}, отправлено {sent}",
        ]
        if report.error:
            lines.append(f"❌ Цикл прерван: {escape(report.error[:200])}")
        for step, reason in report.skipped_steps.items():
            lines.append(f"⚠️ Шаг {step} пропущен: {escape(reason[:150])}")
    if next_run_at is not None:
        lines.append(f"Следующий цикл: {format_local(next_run_at, '%H:%M')}")
    return lines


def build_status_text(
    state: CycleState, next_run_at: datetime | None = None, engine: Engine | None = None
) -> str:
    settings = get_settings()
    day_start = _local_day_start_utc()
    with session_scope(engine) as session:
        lines = ["📡 <b>Радар: состояние</b>", *_source_lines(session), ""]
        lines += _cycle_lines(state, next_run_at)
        lines += [
            "",
            f"Сегодня: событий {_count(session, Event.id, Event.first_seen_at >= day_start)}, "
            f"уведомлений {_count(session, Notification.id, Notification.sent_at >= day_start)}",
            f"Интервал {settings.poll_interval_minutes} мин · модель {escape(settings.llm_model)}",
        ]
    return "\n".join(lines)


def build_today_text(engine: Engine | None = None) -> str:
    settings = get_settings()
    day_start = _local_day_start_utc()
    with session_scope(engine) as session:
        collected = _count(session, RawItem.id, RawItem.fetched_at >= day_start)
        analyzed = _count(session, Analysis.id, Analysis.created_at >= day_start)
        notifications = _count(session, Notification.id, Notification.sent_at >= day_start)
        events = session.scalars(
            select(Event)
            .where(Event.first_seen_at >= day_start)
            .order_by(Event.max_importance.desc(), Event.first_seen_at.desc())
        ).all()
        reactions = dict(
            session.execute(
                select(Feedback.action, func.count())
                .where(Feedback.created_at >= day_start)
                .group_by(Feedback.action)
            ).all()
        )

        lines = [
            f"🗓 <b>Сводка за {format_local(utcnow(), '%d.%m')}</b>",
            f"Собрано материалов {collected}, проанализировано {analyzed}",
            f"Событий {len(events)}, уведомлений {notifications}",
            "Реакции: " + " · ".join(
                f"{label} {reactions.get(action, 0)}" for action, label in ACTION_LABELS.items()
            ),
        ]
        shown = 0
        for event in events:
            origin = session.scalar(
                select(EventItem).where(EventItem.event_id == event.id, EventItem.relation == "origin")
            )
            if origin is None or origin.raw_item.source.role == "own":
                continue
            if shown == 0:
                lines += ["", "<b>Главное:</b>"]
            analysis = {**origin.raw_item.analysis.result, "importance": event.max_importance}
            title = escape(event.headline)
            if origin.raw_item.url:
                title = f'<a href="{escape(origin.raw_item.url, quote=True)}">{title}</a>'
            sources = _count(session, EventItem.id, EventItem.event_id == event.id)
            more = f" · источников: {sources}" if sources > 1 else ""
            lines.append(f"{marker_for(analysis, settings)} [{event.max_importance}/5] {title}{more}")
            shown += 1
            if shown == TOP_EVENTS:
                break
        if shown == 0:
            lines += ["", "Событий от внешних источников сегодня пока нет."]
    return "\n".join(lines)
