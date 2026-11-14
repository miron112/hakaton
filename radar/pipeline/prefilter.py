"""Дешёвый отбор до LLM. Географию здесь не трогаем — её решает модель."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, select

from radar.config import Settings, get_settings
from radar.db import session_scope
from radar.log import log_event
from radar.models import RawItem, utcnow


@dataclass
class PrefilterStats:
    checked: int = 0
    passed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def reject_reason(
    title: str | None, text: str, published_at: datetime | None, settings: Settings,
    now: datetime | None = None,
) -> str | None:
    """Причина отсева или None, если материал идёт дальше."""
    full_text = f"{title or ''} {text}".strip()
    if len(full_text) < settings.min_text_len:
        return "too_short"
    age_limit = (now or utcnow()) - timedelta(hours=settings.max_age_hours)
    if published_at is not None and published_at < age_limit:
        return "too_old"
    lowered = full_text.lower()
    if any(word in lowered for word in settings.stop_word_list):
        return "stop_word"
    return None


def prefilter_new(engine: Engine | None = None) -> PrefilterStats:
    """Помечает filtered_out материалы со статусом new, не прошедшие отбор."""
    settings = get_settings()
    stats = PrefilterStats()
    with session_scope(engine) as session:
        for item in session.scalars(select(RawItem).where(RawItem.status == "new")):
            stats.checked += 1
            reason = reject_reason(item.title, item.text, item.published_at, settings)
            if reason is None:
                stats.passed += 1
                continue
            item.status = "filtered_out"
            stats.reasons[reason] = stats.reasons.get(reason, 0) + 1
    log_event("prefilter", "ok", checked=stats.checked, passed=stats.passed, **stats.reasons)
    return stats
