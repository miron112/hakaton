"""Сборщик RSS/Atom-лент. С опцией full_text догружает полный текст статьи по ссылке."""

import asyncio
import logging
from calendar import timegm
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from radar.collectors.base import CollectedItem, Collector, clean_text, html_to_text
from radar.config import get_settings
from radar.log import log_event
from radar.models import Source, utcnow


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
    time_budget_factor = 6.0  # с full_text кроме ленты качаются статьи

    async def _add_full_text(
        self, item: CollectedItem, source: Source, semaphore: asyncio.Semaphore
    ) -> None:
        """Заменяет краткое описание из ленты текстом статьи. При сбое остаётся описание."""
        from radar.collectors.html import extract_article  # trafilatura тяжёлая, грузим по надобности

        try:
            async with semaphore:
                response = await self.client.get(item.url)
                response.raise_for_status()
            article = extract_article(response.text, item.url)
        except Exception as error:
            log_event(
                "collect", "article_error", source.name, logging.WARNING,
                url=item.url, error=f"{type(error).__name__}: {error}"[:200],
            )
            return
        if article and len(article["text"]) > len(item.text):
            item.text = article["text"][: get_settings().llm_max_text_chars]

    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        response = await self.client.get(source.url)
        response.raise_for_status()
        items = parse_feed(response.content)
        if not (source.parser_config or {}).get("full_text"):
            return items

        settings = get_settings()
        # Старое и уже сохранённое не трогаем: полный текст нужен только тому, что пойдёт в анализ
        fresh_after = utcnow() - timedelta(hours=settings.initial_lookback_hours)
        to_load = [
            item for item in items
            if item.url
            and item.external_id not in known_ids
            and (item.published_at is None or item.published_at >= fresh_after)
        ][: settings.rss_full_text_max_per_cycle]
        semaphore = asyncio.Semaphore(settings.html_article_concurrency)
        await asyncio.gather(*(self._add_full_text(item, source, semaphore) for item in to_load))
        return items
