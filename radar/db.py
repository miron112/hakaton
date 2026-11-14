"""Подключение к БД и сессии."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from radar.config import get_settings
from radar.models import Base


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        # Бот и админка открывают один файл: ждём блокировку, а не падаем
        connect_args["timeout"] = 30
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        url,
        connect_args=connect_args,
        # Кириллица в JSON-колонках хранится как есть, а не \uXXXX — базу удобнее читать
        json_serializer=lambda value: json.dumps(value, ensure_ascii=False),
    )


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def _add_missing_columns(engine: Engine) -> None:
    """Мини-миграция прототипа: дописывает новые необязательные колонки в существующие таблицы."""
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing and column.nullable:
                    column_type = column.type.compile(engine.dialect)
                    connection.execute(
                        text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column_type}')
                    )


def init_db(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Сессия с коммитом при успехе и откатом при ошибке."""
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
