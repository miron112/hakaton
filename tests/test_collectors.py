"""Парсеры на сохранённых страницах (tests/fixtures), без сети."""

import re
from datetime import datetime
from pathlib import Path

import pytest

from radar.collectors.base import clean_text, html_to_text
from radar.collectors.rss import parse_feed
from radar.collectors.telegram_web import parse_channel_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_telegram_page_is_parsed() -> None:
    html = (FIXTURES / "telegram_admsurgut.html").read_text(encoding="utf-8")
    items = parse_channel_page(html)

    assert len(items) == 10
    first = items[0]
    assert first.external_id == "admsurgut/19287"
    assert first.url == "https://t.me/admsurgut/19287"
    assert first.published_at == datetime(2026, 8, 8, 4, 0, 25)
    assert first.title is None
    for item in items:
        assert re.fullmatch(r"admsurgut/\d+", item.external_id)
        assert len(item.text) > 20 and "<" not in item.text
        assert item.published_at is not None and item.published_at.tzinfo is None
    assert len({item.external_id for item in items}) == len(items)


def test_telegram_page_without_posts_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse_channel_page("<html><body>Channel not found</body></html>")


def test_telegram_text_keeps_line_breaks_and_skips_media_only_posts() -> None:
    html = """
    <div class="tgme_widget_message" data-post="chan/1">
      <div class="tgme_widget_message_text js-message_text">Первая строка<br/>Вторая <b>строка</b></div>
      <a class="tgme_widget_message_date"><time datetime="2026-11-14T10:00:00+05:00"></time></a>
    </div>
    <div class="tgme_widget_message" data-post="chan/2">
      <a class="tgme_widget_message_photo_wrap"></a>
    </div>
    """
    items = parse_channel_page(html)

    assert [item.external_id for item in items] == ["chan/1"]
    assert items[0].text == "Первая строка\nВторая строка"
    assert items[0].published_at == datetime(2026, 11, 14, 5, 0, 0)  # приведено к UTC


@pytest.mark.parametrize("name", ["rss_86ru.xml", "rss_siapress.xml"])
def test_rss_feeds_are_parsed(name: str) -> None:
    items = parse_feed((FIXTURES / name).read_bytes())

    assert len(items) >= 10
    for item in items:
        assert item.url and item.url.startswith("https://")
        assert item.external_id
        assert item.title and item.title == item.title.strip() and "\n" not in item.title
        assert "​" not in item.title
        assert "<" not in item.text
        assert item.published_at is not None and item.published_at.tzinfo is None
    assert len({item.external_id for item in items}) == len(items)


def test_rss_dates_are_converted_to_utc() -> None:
    items = parse_feed((FIXTURES / "rss_siapress.xml").read_bytes())
    # В ленте: Sat, 14 Nov 2026 13:32:29 +0500
    assert items[0].published_at == datetime(2026, 11, 14, 8, 32, 29)


def test_broken_feed_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse_feed(b"<html><body>502 Bad Gateway</body></html")


def test_text_cleaning() -> None:
    assert clean_text("​ Заголовок \n\n\n  текст тут ") == "Заголовок\n\nтекст тут"
    assert html_to_text('<p><img src="a.jpg"/></p>Суть <b>новости</b>') == "Суть новости"
    assert html_to_text(None) == ""
