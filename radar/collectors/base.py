"""Общий интерфейс сборщиков и помощники для очистки текста."""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.models import Source

_INVISIBLE = re.compile(r"[​‌‍⁠﻿­]")
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_WHITESPACE = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    """Убирает невидимые символы и лишние пробелы, сохраняя переносы абзацев."""
    text = _INVISIBLE.sub("", value or "")
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    for node in tree.css("br"):
        node.replace_with("\n")
    root = tree.body or tree.root
    return clean_text(root.text(separator=" ") if root else "")


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class CollectedItem:
    """Материал, полученный из источника, до записи в raw_items."""

    external_id: str
    url: str | None
    title: str | None
    text: str
    published_at: datetime | None  # naive UTC

    @property
    def content_hash(self) -> str:
        normalized = _WHITESPACE.sub(" ", f"{self.title or ''} {self.text}").strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Collector(ABC):
    """Сборщик одного типа источников. Ошибки наружу — их ловит цикл сбора."""

    # Во сколько раз больше обычного таймаута даётся на источник (html качает ещё и статьи)
    time_budget_factor: float = 1.0

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    @abstractmethod
    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        """known_ids — external_id уже сохранённых материалов: их можно не скачивать заново."""
