"""Сборщик RSS/Atom-лент."""

from calendar import timegm
from datetime import datetime, timezone
from typing import Any

import feedparser

from radar.collectors.base import CollectedItem, Collector, clean_text, html_to_text
from radar.models import Source


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    # feedparser отдаёт время уже в UTC
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc).replace(tzinfo=None)


def _body(entry: Any) -> str:
    content = entry.get("content") or []
    html = content[0].get("value") if content else entry.get("summary")
    return html_to_text(html)


def parse_feed(content: bytes) -> list[CollectedItem]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"не удалось разобрать ленту: {feed.bozo_exception}")

    items: list[CollectedItem] = []
    for entry in feed.entries:
        url = clean_text(entry.get("link")) or None
        external_id = clean_text(entry.get("id")) or url
        title = clean_text(entry.get("title")).replace("\n", " ")
        if not external_id or not (title or _body(entry)):
            continue
        items.append(
            CollectedItem(
                external_id=external_id,
                url=url,
                title=title or None,
                text=_body(entry),
                published_at=_published_at(entry),
            )
        )
    return items


class RssCollector(Collector):
    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        response = await self.client.get(source.url)
        response.raise_for_status()
        return parse_feed(response.content)
