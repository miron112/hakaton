"""Цикл сбора: параллельно опрашивает источники и сохраняет новые материалы в raw_items."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from radar.collectors import COLLECTORS, CollectedItem
from radar.config import Settings, get_settings
from radar.db import session_scope
from radar.log import log_event
from radar.models import RawItem, Source, utcnow


@dataclass
class SourceResult:
    source_id: int
    source_name: str
    fetched: int = 0
    new: int = 0
    old: int = 0  # старше окна первого запуска: сохранены, но в обработку не идут
    duplicates: int = 0
    error: str | None = None
    skipped_reason: str | None = None
    new_item_ids: list[int] = field(default_factory=list)


def _describe(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "таймаут"
    return f"{type(error).__name__}: {error}"[:500]


async def _fetch_with_retries(
    source: Source, known_ids: frozenset[str], client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore, settings: Settings,
) -> list[CollectedItem]:
    collector = COLLECTORS[source.type](client)
    attempts = settings.fetch_retries + 1
    budget = settings.fetch_timeout_seconds * collector.time_budget_factor
    async with semaphore:
        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(budget):
                    return await collector.fetch(source, known_ids)
            except Exception as error:
                if attempt == attempts:
                    raise
                log_event(
                    "collect", "retry", source.name, logging.WARNING,
                    attempt=attempt, error=_describe(error),
                )
                await asyncio.sleep(settings.fetch_retry_pause_seconds)
    raise RuntimeError("unreachable")


KNOWN_IDS_LIMIT = 500


def _recent_external_ids(session: Session, source_id: int) -> frozenset[str]:
    """Последние сохранённые материалы источника — чтобы сборщик не качал их повторно."""
    return frozenset(
        session.scalars(
            select(RawItem.external_id)
            .where(RawItem.source_id == source_id)
            .order_by(RawItem.id.desc())
            .limit(KNOWN_IDS_LIMIT)
        ).all()
    )


def _known(session: Session, column, values: list[str]) -> set[str]:
    if not values:
        return set()
    return set(session.scalars(select(column).where(column.in_(values))).all())


def _save_items(
    session: Session, source: Source, items: list[CollectedItem], settings: Settings
) -> SourceResult:
    result = SourceResult(source.id, source.name, fetched=len(items))
    known_ids = set(
        session.scalars(
            select(RawItem.external_id).where(
                RawItem.source_id == source.id,
                RawItem.external_id.in_([item.external_id for item in items]),
            )
        ).all()
    ) if items else set()
    known_hashes = _known(session, RawItem.content_hash, [item.content_hash for item in items])

    # Первый запуск: старое сохраняем как filtered_out, чтобы оно не всплыло «новым» позже
    first_run = source.last_success_at is None
    cutoff = utcnow() - timedelta(hours=settings.initial_lookback_hours)
    created: list[RawItem] = []
    for item in items:
        if item.external_id in known_ids or item.content_hash in known_hashes:
            result.duplicates += 1
            continue
        known_ids.add(item.external_id)
        known_hashes.add(item.content_hash)
        is_old = first_run and item.published_at is not None and item.published_at < cutoff
        row = RawItem(
            source_id=source.id,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            text=item.text,
            published_at=item.published_at,
            content_hash=item.content_hash,
            status="filtered_out" if is_old else "new",
        )
        session.add(row)
        if is_old:
            result.old += 1
        else:
            created.append(row)

    source.last_success_at = utcnow()
    source.last_error = None
    source.error_count = 0
    session.flush()
    result.new = len(created)
    result.new_item_ids = [row.id for row in created]
    return result


def _store_outcome(
    engine: Engine | None, source_id: int, outcome: list[CollectedItem] | BaseException,
    settings: Settings,
) -> SourceResult:
    with session_scope(engine) as session:
        source = session.get_one(Source, source_id)
        if isinstance(outcome, BaseException):
            source.last_error = _describe(outcome)
            source.error_count += 1
            log_event("collect", "error", source.name, logging.ERROR, error=source.last_error)
            return SourceResult(source.id, source.name, error=source.last_error)
        result = _save_items(session, source, outcome, settings)
    log_event(
        "collect", "ok", result.source_name,
        fetched=result.fetched, new=result.new, old=result.old, duplicates=result.duplicates,
    )
    return result


async def collect_all(engine: Engine | None = None) -> list[SourceResult]:
    """Опрашивает все включённые источники. Сбой одного не влияет на остальные."""
    settings = get_settings()
    with session_scope(engine) as session:
        sources = session.scalars(
            select(Source).where(Source.enabled.is_(True)).order_by(Source.priority, Source.id)
        ).all()
        known_ids = {source.id: _recent_external_ids(session, source.id) for source in sources}

    supported = [source for source in sources if source.type in COLLECTORS]
    results = [
        SourceResult(source.id, source.name, skipped_reason=f"нет сборщика для типа {source.type}")
        for source in sources
        if source.type not in COLLECTORS
    ]

    semaphore = asyncio.Semaphore(settings.fetch_concurrency)
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.fetch_timeout_seconds,
        follow_redirects=True,
    ) as client:
        outcomes = await asyncio.gather(
            *(
                _fetch_with_retries(source, known_ids[source.id], client, semaphore, settings)
                for source in supported
            ),
            return_exceptions=True,
        )

    for source, outcome in zip(supported, outcomes):
        try:
            results.append(_store_outcome(engine, source.id, outcome, settings))
        except Exception as error:  # сбой записи одного источника не должен ронять цикл
            log_event("collect", "save_error", source.name, logging.ERROR, error=_describe(error))
            results.append(SourceResult(source.id, source.name, error=_describe(error)))
    return results
