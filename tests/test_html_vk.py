"""Сборщики HTML и VK. Сеть подменена, страницы — придуманные заглушки на example.com."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from radar.collectors.html import HtmlCollector, _published_at, parse_article, parse_listing
from radar.collectors.rss import RssCollector
from radar.collectors.vk import VkCollector, parse_wall, wall_params
from radar.config import get_settings
from radar.models import Source, utcnow

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "html_listing_stub.html").read_text(encoding="utf-8")
ARTICLE = (FIXTURES / "html_article_stub.html").read_text(encoding="utf-8")
PARSER = {"list_selector": "div.news-item", "link_selector": "a.title"}
BASE = "https://example.com/news"


# --- HTML ---

def test_listing_links_are_absolute_unique_and_ordered() -> None:
    links = parse_listing(LISTING, BASE, PARSER)

    assert [url for url, _ in links] == [
        "https://example.com/news/101-avariya-na-setyah",
        "https://example.com/news/102-remont-dorogi",  # без #comments
        "https://example.com/news/103-bitaya-statya",
    ]
    assert links[0][1] == "Авария на сетях: без воды остались два микрорайона"
    assert links[1][1] == "Ремонт дороги начнётся в понедельник"


def test_listing_respects_max_items_and_reports_bad_selectors() -> None:
    assert len(parse_listing(LISTING, BASE, {**PARSER, "max_items": 1})) == 1
    with pytest.raises(ValueError, match="div.missing"):
        parse_listing(LISTING, BASE, {**PARSER, "list_selector": "div.missing"})
    with pytest.raises(ValueError, match="a.missing"):
        parse_listing(LISTING, BASE, {**PARSER, "link_selector": "a.missing"})


def test_article_text_is_extracted_without_page_chrome() -> None:
    item = parse_article(ARTICLE, f"{BASE}/101", "Заголовок из ленты")

    assert item is not None
    assert item.title == "Заголовок из ленты" and item.external_id == f"{BASE}/101"
    assert "Аварийная бригада уже работает на месте" in item.text
    assert "Политика конфиденциальности" not in item.text and "<p>" not in item.text
    assert item.published_at is not None and item.published_at.date() == datetime(2026, 11, 14).date()
    assert parse_article("<html><body></body></html>", BASE, "x") is None


def test_date_only_becomes_local_noon_but_never_in_the_future() -> None:
    assert _published_at("2026-01-10") == datetime(2026, 1, 10, 7, 0)  # 12:00 UTC+5
    assert _published_at("2026-01-10T09:15:00+05:00") == datetime(2026, 1, 10, 4, 15)
    assert _published_at("2999-01-01") < datetime(2999, 1, 1)
    assert _published_at(None) is None and _published_at("вчера") is None


def _html_source() -> Source:
    return Source(id=1, name="Сайт", type="html", url=BASE, geo="surgut_city", parser_config=PARSER)


def _html_client(requested: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/news":
            return httpx.Response(200, text=LISTING)
        if "103" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, text=ARTICLE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_html_collector_skips_known_and_survives_broken_article() -> None:
    requested: list[str] = []

    async def run(known: frozenset[str]):
        async with _html_client(requested) as client:
            return await HtmlCollector(client).fetch(_html_source(), known)

    items = asyncio.run(run(frozenset()))
    assert [item.url for item in items] == [
        "https://example.com/news/101-avariya-na-setyah",
        "https://example.com/news/102-remont-dorogi",
    ]  # статья 103 вернула 500 и пропущена, остальные собраны

    requested.clear()
    items = asyncio.run(run(frozenset(item.external_id for item in items)))
    assert items == []
    assert sorted(requested) == [BASE, "https://example.com/news/103-bitaya-statya"]  # известные не качаем


# --- RSS с полным текстом ---

def _feed_entry(key: str, hours_ago: int) -> str:
    published = (utcnow() - timedelta(hours=hours_ago)).replace(tzinfo=timezone.utc)
    return (
        f"<item><title>Новость {key}</title><link>https://example.com/a/{key}</link>"
        f"<guid>{key}</guid><pubDate>{format_datetime(published)}</pubDate>"
        f"<description>Кратко.</description></item>"
    )


def test_rss_full_text_replaces_short_description_for_new_fresh_items() -> None:
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>'
        + _feed_entry("new", 1) + _feed_entry("known", 1) + _feed_entry("old", 100)
        + _feed_entry("broken", 1) + "</channel></rss>"
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/rss":
            return httpx.Response(200, content=feed.encode())
        if request.url.path.endswith("broken"):
            return httpx.Response(503)
        return httpx.Response(200, text=ARTICLE)

    async def run(parser_config):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            source = Source(id=1, name="Лента", type="rss", url="https://example.com/rss",
                            geo="khmao", parser_config=parser_config)
            items = await RssCollector(client).fetch(source, frozenset({"known"}))
            return {item.external_id: item for item in items}

    items = asyncio.run(run({"full_text": True}))
    assert "Аварийная бригада уже работает на месте" in items["new"].text
    assert items["known"].text == items["old"].text == items["broken"].text == "Кратко."
    assert sorted(requested) == ["/a/broken", "/a/new", "/rss"]  # известное и старое не качаем

    requested.clear()
    items = asyncio.run(run(None))
    assert items["new"].text == "Кратко." and requested == ["/rss"]


# --- VK ---

WALL = {
    "response": {
        "count": 4,
        "items": [
            {"id": 10, "owner_id": -123, "date": 1794643200, "text": "В Сургуте открыли новый детский сад на 300 мест."},
            {"id": 11, "owner_id": -123, "date": 1794646800, "text": "", "attachments": [{"type": "photo"}]},
            {"id": 12, "owner_id": -123, "date": 1794650400, "text": "Важно!",
             "copy_history": [{"text": "С понедельника меняется расписание автобусов."}]},
            {"id": 13, "owner_id": -123, "date": 1794654000, "text": "Скидки до 50%", "marked_as_ads": 1},
        ],
    }
}


def test_wall_is_parsed() -> None:
    items = parse_wall(WALL)

    assert [item.external_id for item in items] == ["-123_10", "-123_12"]  # без пустого и рекламного
    assert items[0].url == "https://vk.com/wall-123_10"
    assert items[0].published_at == datetime(2026, 11, 14, 8, 0)
    assert items[1].text == "Важно!\n\nС понедельника меняется расписание автобусов."


def test_vk_api_error_is_raised_with_message() -> None:
    with pytest.raises(ValueError, match="User authorization failed"):
        parse_wall({"error": {"error_code": 5, "error_msg": "User authorization failed"}})


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("example_club", {"domain": "example_club"}),
        ("club123", {"owner_id": "-123"}),
        ("public45", {"owner_id": "-45"}),
        ("-678", {"owner_id": "-678"}),
    ],
)
def test_wall_params(handle: str, expected: dict) -> None:
    assert wall_params(handle) == expected


def test_vk_collector_sends_token_in_body_not_in_url(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "vk_token", "secret-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=WALL)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            source = Source(id=1, name="Стена", type="vk", url="example_club", geo="surgut_city")
            return await VkCollector(client).fetch(source)

    assert len(asyncio.run(run())) == 2
    request = seen[0]
    assert "secret-token" not in str(request.url)
    body = request.content.decode()
    assert "access_token=secret-token" in body and "domain=example_club" in body


def test_vk_collector_without_token_fails_clearly(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "vk_token", None)

    async def run():
        async with httpx.AsyncClient() as client:
            source = Source(id=1, name="Стена", type="vk", url="example_club", geo="surgut_city")
            await VkCollector(client).fetch(source)

    with pytest.raises(RuntimeError, match="VK_TOKEN"):
        asyncio.run(run())
