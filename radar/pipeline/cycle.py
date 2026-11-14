"""Один полный цикл: сбор → предфильтр → анализ → события → уведомления."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine

from radar.bot.notifier import NotifyStats, Sender, notify_pending
from radar.llm import LLMClient
from radar.log import log_event
from radar.models import utcnow
from radar.pipeline.analyze import AnalyzeStats, analyze_pending
from radar.pipeline.collect import SourceResult, collect_all
from radar.pipeline.dedup import DedupStats, assign_events
from radar.pipeline.embeddings import Embedder
from radar.pipeline.prefilter import PrefilterStats, prefilter_new


@dataclass
class CycleReport:
    started_at: datetime
    duration_seconds: float = 0.0
    collect: list[SourceResult] = field(default_factory=list)
    prefilter: PrefilterStats | None = None
    analyze: AnalyzeStats | None = None
    dedup: DedupStats | None = None
    notify: NotifyStats | None = None
    skipped_steps: dict[str, str] = field(default_factory=dict)  # шаг → почему пропущен
    error: str | None = None

    @property
    def new_items(self) -> int:
        return sum(result.new for result in self.collect)

    @property
    def failed_sources(self) -> int:
        return sum(1 for result in self.collect if result.error)


@dataclass
class CycleState:
    """Состояние для /status. Живёт в памяти процесса планировщика."""

    running: bool = False
    cycles_done: int = 0
    last_report: CycleReport | None = None
    next_run_at: datetime | None = None  # naive UTC; заполняет планировщик


state = CycleState()


async def run_cycle(
    client: LLMClient | None,
    embedder: Embedder | None,
    sender: Sender | None,
    engine: Engine | None = None,
    skip_reasons: dict[str, str] | None = None,
) -> CycleReport:
    """Шаги без нужного сервиса (LLM, эмбеддинги, Telegram) пропускаются, остальные работают.

    Исключения наружу не выходят: ошибка пишется в отчёт и лог, следующий цикл начнётся с чистого листа.
    """
    report = CycleReport(started_at=utcnow(), skipped_steps=dict(skip_reasons or {}))
    started = time.monotonic()
    state.running = True
    try:
        report.collect = await collect_all(engine)
        report.prefilter = prefilter_new(engine)
        if client is not None:
            report.analyze = await analyze_pending(client, engine)
            if embedder is not None:
                report.dedup = await assign_events(embedder, client, engine)
        if sender is not None:
            report.notify = await notify_pending(sender, engine)
    except Exception as error:
        report.error = f"{type(error).__name__}: {error}"[:500]
        logging.getLogger("radar").exception("Цикл прерван ошибкой")
    finally:
        report.duration_seconds = round(time.monotonic() - started, 1)
        state.running = False
        state.cycles_done += 1
        state.last_report = report

    notify = report.notify
    log_event(
        "cycle", "error" if report.error else "ok", seconds=report.duration_seconds,
        sources=len(report.collect), failed_sources=report.failed_sources,
        new_items=report.new_items,
        analyzed=report.analyze.analyzed if report.analyze else 0,
        new_events=report.dedup.new_events if report.dedup else 0,
        sent=(notify.sent + notify.updates + notify.in_digest) if notify else 0,
    )
    return report
