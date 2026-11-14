"""Сборщик публичных Telegram-каналов через веб-превью https://t.me/s/<channel>."""

from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from radar.collectors.base import CollectedItem, Collector, clean_text, to_naive_utc
from radar.models import Source

PREVIEW_URL = "https://t.me/s/{handle}"


def _message_text(message: Node) -> str:
    # В ответах и репостах блоков текста несколько; текст самого поста — последний
    blocks = message.css(".tgme_widget_message_text.js-message_text")
    if not blocks:
        return ""
    block = blocks[-1]
    for node in block.css("br"):
        node.replace_with("\n")
    return clean_text(block.text())


def _published_at(message: Node) -> datetime | None:
    node = message.css_first("a.tgme_widget_message_date time")
    raw = node.attributes.get("datetime") if node else None
    if not raw:
        return None
    try:
        return to_naive_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def parse_channel_page(html: str) -> list[CollectedItem]:
    """Разбирает страницу превью. Посты без текста (только фото/видео) пропускаются."""
    messages = HTMLParser(html).css("div.tgme_widget_message[data-post]")
    if not messages:
        raise ValueError("на странице нет постов: канал закрыт, не существует или без веб-превью")

    items: list[CollectedItem] = []
    for message in messages:
        post = message.attributes.get("data-post") or ""
        text = _message_text(message)
        if not post or not text:
            continue
        items.append(
            CollectedItem(
                external_id=post,
                url=f"https://t.me/{post}",
                title=None,
                text=text,
                published_at=_published_at(message),
            )
        )
    return items


class TelegramWebCollector(Collector):
    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        response = await self.client.get(PREVIEW_URL.format(handle=source.url))
        response.raise_for_status()
        return parse_channel_page(response.text)
