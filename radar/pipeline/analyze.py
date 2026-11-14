"""LLM-анализ материала (Промт 1): классификация, важность, заголовок и суть."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import joinedload

from radar.config import Settings, get_settings
from radar.db import session_scope
from radar.llm import LLMClient, LLMResponseError, LLMResult, LLMUsage
from radar.log import log_event
from radar.models import Analysis, RawItem
from radar.prompts import ANALYZE_PROMPT_VERSION, load_prompt, render
from radar.timeutil import format_local

LOW_VALUE_TYPES = ("routine_pr", "ad", "not_news")
LOW_VALUE_MAX_IMPORTANCE = 2
FALLBACK_TOPIC = "другое"


class AnalysisResult(BaseModel):
    """Схема ответа Промта 1."""

    model_config = ConfigDict(extra="ignore")

    is_relevant: bool
    geo_level: Literal["surgut_city", "surgut_district", "khmao", "other", "unknown"]
    affects_surgut: bool = False
    places: list[str] = Field(default_factory=list)
    topic: str = FALLBACK_TOPIC
    content_type: Literal[
        "event", "decision", "announcement", "statement", "data", "routine_pr", "ad", "not_news"
    ]
    importance: int = Field(ge=1, le=5)
    importance_reason: str = ""
    urgency: Literal["breaking", "today", "background"] = "background"
    headline: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_facts: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    needs_verification: bool = False
    verification_note: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass
class AnalyzeStats:
    analyzed: int = 0
    invalid: int = 0  # модель дважды вернула невалидный JSON → status=error
    failed: int = 0  # сбой API или сети → материал остаётся new до следующего цикла
    postponed: int = 0  # не поместились в лимит цикла
    usage: LLMUsage = field(default_factory=LLMUsage)
    analyzed_item_ids: list[int] = field(default_factory=list)


@dataclass
class _Job:
    item_id: int
    source_name: str
    user_prompt: str


def build_system_prompt(settings: Settings) -> str:
    return render(
        load_prompt("analyze_system"),
        EDITORIAL_PRIORITIES=settings.editorial_priorities,
        EXCLUDED_TOPICS=settings.excluded_topics,
        TOPICS=", ".join(settings.topic_list),
    )


def build_user_prompt(item: RawItem, settings: Settings) -> str:
    source = item.source
    text = f"{item.title}\n\n{item.text}" if item.title else item.text
    return render(
        load_prompt("analyze_user"),
        source_name=source.name,
        source_type=source.type,
        source_priority=source.priority,
        source_note=source.note or "без пометок",
        published_at=f"{format_local(item.published_at)} (время Сургута)",
        url=item.url or "нет",
        text=text[: settings.llm_max_text_chars],
    )


def normalize(data: dict, settings: Settings) -> dict:
    """Правила, которые надёжнее держать в коде, чем в промте."""
    if data["content_type"] in LOW_VALUE_TYPES:
        data["importance"] = min(data["importance"], LOW_VALUE_MAX_IMPORTANCE)
    by_lower = {topic.lower(): topic for topic in settings.topic_list}
    data["topic"] = by_lower.get(str(data["topic"]).strip().lower(), FALLBACK_TOPIC)
    data["headline"] = " ".join(data["headline"].split())
    return data


def _load_jobs(engine: Engine | None, settings: Settings) -> tuple[list[_Job], int]:
    with session_scope(engine) as session:
        items = session.scalars(
            select(RawItem)
            .options(joinedload(RawItem.source))
            .where(RawItem.status == "new")
            .order_by(RawItem.published_at.desc())
        ).all()
        # При нехватке лимита первыми идут важные источники, внутри — свежие материалы
        items = sorted(items, key=lambda item: item.source.priority)
        limit = settings.llm_max_items_per_cycle
        jobs = [
            _Job(item.id, item.source.name, build_user_prompt(item, settings))
            for item in items[:limit]
        ]
        return jobs, max(0, len(items) - limit)


async def _run_job(
    job: _Job, client: LLMClient, system_prompt: str, semaphore: asyncio.Semaphore
) -> LLMResult:
    async with semaphore:
        return await client.complete_json(system_prompt, job.user_prompt, AnalysisResult)


def _save(
    engine: Engine | None, job: _Job, outcome: LLMResult | BaseException, client: LLMClient,
    settings: Settings, stats: AnalyzeStats,
) -> None:
    if isinstance(outcome, LLMResponseError):
        with session_scope(engine) as session:
            session.get_one(RawItem, job.item_id).status = "error"
        stats.invalid += 1
        log_event(
            "analyze", "invalid_json", job.source_name, logging.ERROR,
            item=job.item_id, error=str(outcome)[:300],
        )
        return
    if isinstance(outcome, BaseException):
        stats.failed += 1
        log_event(
            "analyze", "api_error", job.source_name, logging.ERROR,
            item=job.item_id, error=f"{type(outcome).__name__}: {outcome}"[:300],
        )
        return

    data = normalize(outcome.data, settings)
    with session_scope(engine) as session:
        session.add(
            Analysis(
                raw_item_id=job.item_id,
                result=data,
                model=client.model,
                prompt_version=ANALYZE_PROMPT_VERSION,
            )
        )
        session.get_one(RawItem, job.item_id).status = "analyzed"
    stats.analyzed += 1
    stats.analyzed_item_ids.append(job.item_id)
    stats.usage.requests += outcome.usage.requests
    stats.usage.prompt_tokens += outcome.usage.prompt_tokens
    stats.usage.completion_tokens += outcome.usage.completion_tokens
    stats.usage.cost += outcome.usage.cost
    log_event(
        "analyze", "ok", job.source_name, item=job.item_id,
        relevant=data["is_relevant"], importance=data["importance"], geo=data["geo_level"],
        tokens_in=outcome.usage.prompt_tokens, tokens_out=outcome.usage.completion_tokens,
    )


async def analyze_pending(client: LLMClient, engine: Engine | None = None) -> AnalyzeStats:
    """Анализирует материалы со статусом new. Сбой одного материала не влияет на остальные."""
    settings = get_settings()
    jobs, postponed = _load_jobs(engine, settings)
    stats = AnalyzeStats(postponed=postponed)
    if not jobs:
        return stats

    system_prompt = build_system_prompt(settings)
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    outcomes = await asyncio.gather(
        *(_run_job(job, client, system_prompt, semaphore) for job in jobs),
        return_exceptions=True,
    )
    for job, outcome in zip(jobs, outcomes):
        _save(engine, job, outcome, client, settings, stats)

    log_event(
        "analyze", "cycle", model=client.model, analyzed=stats.analyzed, invalid=stats.invalid,
        failed=stats.failed, postponed=stats.postponed, requests=stats.usage.requests,
        tokens_in=stats.usage.prompt_tokens, tokens_out=stats.usage.completion_tokens,
        cost=round(stats.usage.cost, 4),
    )
    return stats
