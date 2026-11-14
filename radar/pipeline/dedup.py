"""Привязка материалов к событиям: эмбеддинги ищут похожие, Промт 2 решает отношение."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, joinedload

from radar.config import Settings, get_settings
from radar.db import session_scope
from radar.llm import LLMClient, LLMResponseError, LLMUsage
from radar.log import log_event
from radar.models import Analysis, Event, EventItem, RawItem, utcnow
from radar.pipeline.embeddings import Embedder, cosine
from radar.prompts import load_prompt, render
from radar.timeutil import format_local

MAX_EVENT_FACTS = 12
OFFICIAL_CONFIRMATION = "Официальное подтверждение"


class DedupResult(BaseModel):
    """Схема ответа Промта 2."""

    model_config = ConfigDict(extra="ignore")

    relation: Literal["duplicate", "update", "new_event"]
    event_id: str | None = None
    new_facts: list[str] = Field(default_factory=list)
    is_official_confirmation: bool = False
    reason: str = ""

    @field_validator("event_id", mode="before")
    @classmethod
    def _id_to_str(cls, value: object) -> str | None:
        return None if value is None else str(value).strip("[] ")


@dataclass
class DedupStats:
    new_events: int = 0
    duplicates: int = 0
    updates: int = 0
    failed: int = 0  # сбой API: материал останется без события до следующего цикла
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass
class Candidate:
    event: Event
    similarity: float
    sources_count: int


def event_text(headline: str, summary: str) -> str:
    return f"{headline}. {summary}"


def _unassigned_ids(session: Session) -> list[int]:
    """Проанализированные релевантные материалы, ещё не привязанные к событию."""
    assigned = select(EventItem.raw_item_id)
    items = session.scalars(
        select(RawItem)
        .join(Analysis)
        .options(joinedload(RawItem.analysis))
        .where(RawItem.status == "analyzed", RawItem.id.not_in(assigned))
        .order_by(RawItem.published_at, RawItem.id)
    ).all()
    return [item.id for item in items if item.analysis.result["is_relevant"]]


def find_candidates(session: Session, embedding: list[float], settings: Settings) -> list[Candidate]:
    """Похожие события за окно дедупликации, лучшие top-k."""
    since = utcnow() - timedelta(hours=settings.dedup_window_hours)
    events = session.scalars(
        select(Event).where(Event.last_updated_at >= since, Event.embedding.is_not(None))
    ).all()
    scored = [(cosine(embedding, event.embedding), event) for event in events if event.embedding]
    scored = [pair for pair in scored if pair[0] >= settings.dedup_sim_threshold]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        Candidate(
            event=event,
            similarity=similarity,
            sources_count=session.scalar(
                select(func.count()).select_from(EventItem).where(EventItem.event_id == event.id)
            ) or 0,
        )
        for similarity, event in scored[: settings.dedup_top_k]
    ]


def build_user_prompt(item: RawItem, candidates: list[Candidate]) -> str:
    result = item.analysis.result
    blocks = [
        f"[{candidate.event.id}] {format_local(candidate.event.first_seen_at)} | {candidate.event.headline}\n"
        f"Суть: {candidate.event.summary}\n"
        f"Факты: {'; '.join(candidate.event.key_facts) or 'нет'}\n"
        f"Источники: {candidate.sources_count}"
        for candidate in candidates
    ]
    return render(
        load_prompt("dedup_user"),
        source_name=item.source.name,
        published_at=format_local(item.published_at),
        headline=result["headline"],
        summary=result["summary"],
        key_facts="; ".join(result["key_facts"]) or "нет",
        candidates="\n\n".join(blocks),
    )


def create_event(session: Session, item: RawItem, embedding: list[float] | None) -> Event:
    result = item.analysis.result
    event = Event(
        headline=result["headline"],
        summary=result["summary"],
        key_facts=result["key_facts"],
        geo_level=result["geo_level"],
        topic=result["topic"],
        max_importance=result["importance"],
        first_seen_at=item.fetched_at,
        last_updated_at=item.fetched_at,
        embedding=embedding,
    )
    session.add(event)
    session.add(EventItem(event=event, raw_item=item, relation="origin"))
    session.flush()
    return event


def _matched_event(decision: DedupResult, candidates: list[Candidate]) -> Event:
    by_id = {str(candidate.event.id): candidate.event for candidate in candidates}
    # Модель иногда путает id — тогда берём самое похожее событие
    return by_id.get(decision.event_id or "", candidates[0].event)


def apply_decision(
    session: Session, item: RawItem, decision: DedupResult, candidates: list[Candidate],
    embedding: list[float],
) -> tuple[str, Event]:
    if decision.relation == "new_event" or not candidates:
        return "new_event", create_event(session, item, embedding)

    event = _matched_event(decision, candidates)
    importance = item.analysis.result["importance"]
    if decision.relation == "duplicate":
        session.add(EventItem(event=event, raw_item=item, relation="duplicate"))
        return "duplicate", event

    facts = [fact.strip() for fact in decision.new_facts if fact.strip()]
    if decision.is_official_confirmation:
        facts.insert(0, OFFICIAL_CONFIRMATION)
    session.add(EventItem(event=event, raw_item=item, relation="update", new_facts=facts))
    merged = list(event.key_facts) + [fact for fact in facts if fact not in event.key_facts]
    event.key_facts = merged[-MAX_EVENT_FACTS:]
    event.max_importance = max(event.max_importance, importance)
    event.last_updated_at = utcnow()
    return "update", event


async def _decide(
    client: LLMClient, item: RawItem, candidates: list[Candidate], stats: DedupStats
) -> DedupResult:
    """Нет кандидатов — новое событие без LLM. Невалидный ответ — тоже новое: лучше дубль, чем пропуск."""
    if not candidates:
        return DedupResult(relation="new_event", reason="нет похожих событий")
    try:
        result = await client.complete_json(
            load_prompt("dedup_system"), build_user_prompt(item, candidates), DedupResult
        )
    except LLMResponseError as error:
        log_event("dedup", "invalid_json", item.source.name, logging.ERROR, item=item.id, error=str(error)[:300])
        return DedupResult(relation="new_event", reason="невалидный ответ модели")
    stats.usage.requests += result.usage.requests
    stats.usage.prompt_tokens += result.usage.prompt_tokens
    stats.usage.completion_tokens += result.usage.completion_tokens
    stats.usage.cost += result.usage.cost
    return DedupResult.model_validate(result.data)


async def _drop_known_facts(
    embedder: Embedder, decision: DedupResult, candidates: list[Candidate], settings: Settings
) -> DedupResult:
    """Страховка от «обновлений» без новизны: модель часто пересказывает уже известное.

    Факты, по смыслу совпадающие с известными фактами события, убираются.
    Не осталось ни одного — это duplicate.
    """
    if decision.relation != "update" or not candidates:
        return decision
    event = _matched_event(decision, candidates)
    facts = [fact.strip() for fact in decision.new_facts if fact.strip()]
    known = [text for text in [event.summary, *event.key_facts] if text]
    if facts and known:
        vectors = await asyncio.to_thread(embedder.embed, facts + known)
        fact_vectors, known_vectors = vectors[: len(facts)], vectors[len(facts) :]
        facts = [
            fact
            for fact, vector in zip(facts, fact_vectors)
            if max(cosine(vector, other) for other in known_vectors) < settings.dedup_fact_sim_threshold
        ]
    if not facts and not decision.is_official_confirmation:
        return decision.model_copy(
            update={"relation": "duplicate", "new_facts": [], "reason": "новых фактов нет"}
        )
    return decision.model_copy(update={"new_facts": facts})


async def _backfill_embeddings(embedder: Embedder, engine: Engine | None, settings: Settings) -> None:
    """События, созданные до появления дедупликации, получают эмбеддинг."""
    since = utcnow() - timedelta(hours=settings.dedup_window_hours)
    with session_scope(engine) as session:
        events = session.scalars(
            select(Event).where(Event.embedding.is_(None), Event.last_updated_at >= since)
        ).all()
        if not events:
            return
        texts = [event_text(event.headline, event.summary) for event in events]
        for event, vector in zip(events, await asyncio.to_thread(embedder.embed, texts)):
            event.embedding = vector


async def assign_events(
    embedder: Embedder, client: LLMClient, engine: Engine | None = None
) -> DedupStats:
    """Привязывает материалы к событиям по одному: следующий может совпасть с только что созданным."""
    settings = get_settings()
    stats = DedupStats()
    await _backfill_embeddings(embedder, engine, settings)
    with session_scope(engine) as session:
        item_ids = _unassigned_ids(session)

    for item_id in item_ids:
        try:
            with session_scope(engine) as session:
                item = session.get_one(RawItem, item_id)
                result = item.analysis.result
                text = event_text(result["headline"], result["summary"])
                embedding = (await asyncio.to_thread(embedder.embed, [text]))[0]
                candidates = find_candidates(session, embedding, settings)
                decision = await _decide(client, item, candidates, stats)
                decision = await _drop_known_facts(embedder, decision, candidates, settings)
                relation, event = apply_decision(session, item, decision, candidates, embedding)
                source_name = item.source.name
                event_id = event.id
        except Exception as error:  # сбой API или БД: материал попробуем в следующем цикле
            stats.failed += 1
            log_event("dedup", "error", level=logging.ERROR, item=item_id, error=f"{type(error).__name__}: {error}"[:300])
            continue
        if relation == "new_event":
            stats.new_events += 1
        elif relation == "duplicate":
            stats.duplicates += 1
        else:
            stats.updates += 1
        log_event(
            "dedup", relation, source_name, item=item_id, event=event_id,
            candidates=len(candidates),
            similarity=round(candidates[0].similarity, 3) if candidates else 0,
            reason=decision.reason[:120],
        )

    log_event(
        "dedup", "cycle", new_events=stats.new_events, duplicates=stats.duplicates,
        updates=stats.updates, failed=stats.failed, requests=stats.usage.requests,
        tokens_in=stats.usage.prompt_tokens, tokens_out=stats.usage.completion_tokens,
        cost=round(stats.usage.cost, 4),
    )
    return stats
