"""Загрузка и валидация sources.yaml, синхронизация с таблицей sources.

Проверка: python -m radar.sources
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.models import Source

SourceType = Literal["rss", "html", "telegram", "vk"]
SourceGeo = Literal["surgut_city", "surgut_district", "khmao"]
SourceRole = Literal["source", "own"]

_TELEGRAM_PREFIX = re.compile(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", re.IGNORECASE)
_VK_PREFIX = re.compile(r"^(https?://)?(m\.)?vk\.(com|ru)/", re.IGNORECASE)
_HANDLE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class ParserConfig(BaseModel):
    """CSS-селекторы для html-источника."""

    model_config = ConfigDict(extra="forbid")

    list_selector: str = Field(min_length=1)  # блок одной новости на странице-ленте
    link_selector: str = Field(min_length=1)  # ссылка на статью внутри блока
    max_items: int = Field(default=20, ge=1, le=100)  # сколько верхних новостей смотреть


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: SourceType
    url: str = Field(min_length=1)
    geo: SourceGeo
    priority: int = Field(default=2, ge=1, le=3)
    enabled: bool = True
    role: SourceRole = "source"  # own — лента самой редакции: не уведомлять, только сравнивать
    note: str | None = None
    parser: ParserConfig | None = None
    full_text: bool = False  # только rss: догружать полный текст статьи по ссылке

    @model_validator(mode="before")
    @classmethod
    def _handle_is_url(cls, data: Any) -> Any:
        # Для telegram/vk в YAML удобнее писать handle; внутри это то же поле url
        if isinstance(data, dict) and "handle" in data:
            data = dict(data)
            data.setdefault("url", data.pop("handle"))
            data.pop("handle", None)
        return data

    @model_validator(mode="after")
    def _check_by_type(self) -> "SourceConfig":
        self.url = self.url.strip()
        if self.type in ("rss", "html"):
            if not self.url.startswith(("http://", "https://")):
                raise ValueError("url должен начинаться с http:// или https://")
        elif self.type == "telegram":
            self.url = _normalize_handle(_TELEGRAM_PREFIX.sub("", self.url).lstrip("@"))
        else:
            self.url = _normalize_handle(_VK_PREFIX.sub("", self.url))
        if self.type == "html" and self.parser is None:
            raise ValueError("для type=html обязателен блок parser с селекторами")
        if self.full_text and self.type != "rss":
            raise ValueError("full_text поддерживается только для type=rss")
        return self

    def parser_config(self) -> dict[str, Any] | None:
        """Что сохраняется в sources.parser_config."""
        if self.parser:
            return self.parser.model_dump()
        return {"full_text": True} if self.full_text else None


def _normalize_handle(value: str) -> str:
    handle = value.split("?")[0].strip("/")
    if not _HANDLE.match(handle):
        raise ValueError(f"не похоже на имя канала или сообщества: {value!r}")
    return handle


def _is_placeholder(entry: dict[str, Any]) -> bool:
    return any(str(entry.get(key, "")).strip().upper().startswith("TODO") for key in ("name", "url", "handle"))


def _describe_error(index: int, entry: Any, error: ValidationError) -> str:
    name = entry.get("name", "?") if isinstance(entry, dict) else "?"
    details = "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'запись'}: {err['msg']}"
        for err in error.errors()
    )
    return f"запись #{index} ({name}): {details}"


@dataclass
class LoadResult:
    sources: list[SourceConfig] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # заглушки TODO
    errors: list[str] = field(default_factory=list)


def load_sources(path: str | Path) -> LoadResult:
    """Читает YAML. Невалидная запись не мешает загрузить остальные."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"{path}: ожидается список источников")

    result = LoadResult()
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(data, start=1):
        if isinstance(entry, dict) and _is_placeholder(entry):
            result.skipped.append(f"запись #{index} ({entry.get('name', '?')}): заглушка TODO")
            continue
        try:
            config = SourceConfig.model_validate(entry)
        except ValidationError as error:
            result.errors.append(_describe_error(index, entry, error))
            continue
        key = (config.type, config.url)
        if key in seen:
            result.errors.append(f"запись #{index} ({config.name}): дубль {config.type} {config.url}")
            continue
        seen.add(key)
        result.sources.append(config)
    return result


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0


def sync_sources(session: Session, configs: list[SourceConfig]) -> SyncResult:
    """Добавляет новые источники и обновляет описание существующих.

    enabled и priority задаются из YAML только при создании: дальше ими управляет
    админка, и перезапуск не должен затирать решения редактора. Источники,
    удалённые из YAML, в базе остаются (на них ссылается история).
    """
    result = SyncResult()
    for config in configs:
        parser_config = config.parser_config()
        source = session.scalar(
            select(Source).where(Source.type == config.type, Source.url == config.url)
        )
        if source is None:
            session.add(
                Source(
                    name=config.name,
                    type=config.type,
                    url=config.url,
                    geo=config.geo,
                    priority=config.priority,
                    enabled=config.enabled,
                    role=config.role,
                    note=config.note,
                    parser_config=parser_config,
                )
            )
            result.created += 1
            continue
        described = (source.name, source.geo, source.role, source.note, source.parser_config)
        if described != (config.name, config.geo, config.role, config.note, parser_config):
            source.name, source.geo, source.note = config.name, config.geo, config.note
            source.role, source.parser_config = config.role, parser_config
            result.updated += 1
    session.flush()
    return result


def main() -> None:
    from radar.config import get_settings
    from radar.db import init_db, session_scope

    settings = get_settings()
    init_db()
    loaded = load_sources(settings.sources_file)
    for line in loaded.skipped:
        print(f"ПРОПУСК  {line}")
    for line in loaded.errors:
        print(f"ОШИБКА   {line}")

    with session_scope() as session:
        synced = sync_sources(session, loaded.sources)
        rows = session.scalars(select(Source).order_by(Source.priority, Source.name)).all()
        print(
            f"\n{settings.sources_file}: валидных {len(loaded.sources)}, "
            f"заглушек {len(loaded.skipped)}, ошибок {len(loaded.errors)}; "
            f"в БД добавлено {synced.created}, обновлено {synced.updated}\n"
        )
        for row in rows:
            state = "вкл " if row.enabled else "выкл"
            state += " own" if row.role == "own" else "    "
            print(f"[{row.id:>3}] {state} p{row.priority} {row.type:<8} {row.geo:<15} {row.name} — {row.url}")
        if not rows:
            print("В базе нет источников. Заполните sources.yaml реальными записями.")


if __name__ == "__main__":
    main()
