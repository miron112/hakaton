"""Сборщик сайтов без RSS: ссылки со страницы-ленты по селекторам, текст статьи — trafilatura."""

import asyncio
import logging
from datetime import date, datetime, time, timezone
from urllib.parse import urldefrag, urljoin

import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.collectors.base import CollectedItem, Collector, clean_text, to_naive_utc
from radar.config import get_settings
from radar.log import log_event
from radar.models import Source, utcnow
from radar.timeutil import LOCAL_TZ

DEFAULT_MAX_ITEMS = 20


def parse_listing(html: str, base_url: str, parser_config: dict) -> list[tuple[str, str]]:
    """Возвращает (url, заголовок) статей в порядке появления на странице, без повторов."""
    blocks = HTMLParser(html).css(parser_config["list_selector"])
    if not blocks:
        raise ValueError(f"по селектору {parser_config['list_selector']!r} ничего не найдено")

    links: dict[str, str] = {}
    for block in blocks:
        node = block.css_first(parser_config["link_selector"])
        href = (node.attributes.get("href") or "").strip() if node else ""
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urldefrag(urljoin(base_url, href)).url
        links.setdefault(url, clean_text(node.text()).replace("\n", " "))
    if not links:
        raise ValueError(f"по селектору {parser_config['link_selector']!r} не найдено ни одной ссылки")
    return list(links.items())[: parser_config.get("max_items", DEFAULT_MAX_ITEMS)]


def _published_at(raw: str | None) -> datetime | None:
    """trafilatura чаще всего знает только день; тогда ставим полдень по Сургуту, но не позже «сейчас»."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if len(raw) > 10:
        return to_naive_utc(parsed)
    noon = datetime.combine(date.fromisoformat(raw), time(12), tzinfo=LOCAL_TZ)
    return min(noon.astimezone(timezone.utc).replace(tzinfo=None), utcnow())


def extract_article(html: str, url: str) -> dict | None:
    """Основной текст и метаданные страницы статьи; None — текст не найден."""
    document = trafilatura.bare_extraction(
        html, url=url, include_comments=False, include_tables=False, with_metadata=True
    )
    if document is None:
        return None
    data = document if isinstance(document, dict) else document.as_dict()
    text = clean_text(data.get("text"))
    return {**data, "text": text} if text else None


def parse_article(html: str, url: str, listing_title: str) -> CollectedItem | None:
    data = extract_article(html, url)
    if data is None:
        return None
    text = data["text"]
    return CollectedItem(
        external_id=url,
        url=url,
        title=listing_title or clean_text(data.get("title")) or None,
        text=text,
        published_at=_published_at(data.get("date")),
    )


class HtmlCollector(Collector):
    time_budget_factor = 6.0

    async def _fetch_article(
        self, url: str, title: str, source: Source, semaphore: asyncio.Semaphore
    ) -> CollectedItem | None:
        try:
            async with semaphore:
                response = await self.client.get(url)
                response.raise_for_status()
            return parse_article(response.text, url, title)
        except Exception as error:  # одна битая статья не должна ронять весь источник
            log_event(
                "collect", "article_error", source.name, logging.WARNING,
                url=url, error=f"{type(error).__name__}: {error}"[:200],
            )
            return None

    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        response = await self.client.get(source.url)
        response.raise_for_status()
        links = parse_listing(response.text, str(response.url), source.parser_config or {})

        new_links = [(url, title) for url, title in links if url not in known_ids]
        semaphore = asyncio.Semaphore(get_settings().html_article_concurrency)
        articles = await asyncio.gather(
            *(self._fetch_article(url, title, source, semaphore) for url, title in new_links)
        )
        return [article for article in articles if article is not None]
