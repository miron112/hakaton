"""Модель данных. Только переносимые типы, чтобы схема работала и на SQLite, и на Postgres.

Все даты хранятся как naive UTC (SQLite не хранит таймзону).
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SOURCE_TYPES = ("rss", "html", "telegram", "vk")
SOURCE_GEOS = ("surgut_city", "surgut_district", "khmao")
RAW_ITEM_STATUSES = ("new", "filtered_out", "analyzed", "error")
EVENT_RELATIONS = ("origin", "duplicate", "update")
FEEDBACK_ACTIONS = ("take", "skip", "in_progress")
NOTIFICATION_TYPES = ("new", "update")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("type", "url", name="uq_sources_type_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(16))
    # rss/html — URL; telegram — имя канала; vk — короткое имя или id стены
    url: Mapped[str] = mapped_column(String(1024))
    geo: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String(16), default="source")
    note: Mapped[str | None] = mapped_column(String(255))
    parser_config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    raw_items: Mapped[list["RawItem"]] = relationship(back_populates="source")


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(1024))
    url: Mapped[str | None] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)

    source: Mapped[Source] = relationship(back_populates="raw_items")
    analysis: Mapped["Analysis | None"] = relationship(back_populates="raw_item")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), unique=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    raw_item: Mapped[RawItem] = relationship(back_populates="analysis")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    key_facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    geo_level: Mapped[str] = mapped_column(String(32))
    topic: Mapped[str] = mapped_column(String(64))
    max_importance: Mapped[int] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    embedding: Mapped[list[float] | None] = mapped_column(JSON)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)

    items: Mapped[list["EventItem"]] = relationship(back_populates="event")
    feedback: Mapped[list["Feedback"]] = relationship(back_populates="event")


class EventItem(Base):
    __tablename__ = "event_items"
    __table_args__ = (UniqueConstraint("event_id", "raw_item_id", name="uq_event_items_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), index=True)
    relation: Mapped[str] = mapped_column(String(16))
    new_facts: Mapped[list[str] | None] = mapped_column(JSON)

    event: Mapped[Event] = relationship(back_populates="items")
    raw_item: Mapped[RawItem] = relationship()


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="feedback")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    type: Mapped[str] = mapped_column(String(16))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Для type=update: какой материал принёс обновление (чтобы не отправить его дважды)
    raw_item_id: Mapped[int | None] = mapped_column(ForeignKey("raw_items.id"))

    event: Mapped[Event] = relationship()
