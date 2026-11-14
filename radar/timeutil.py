"""В базе время хранится в UTC, людям и модели показываем местное (Сургут, UTC+5)."""

from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=5))


def format_local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "неизвестно"
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime(fmt)
