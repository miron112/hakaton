"""Один полный цикл с подробным выводом: python -m radar.run_once"""

import asyncio

from sqlalchemy import select

from radar.config import get_settings
from radar.db import init_db, session_scope
from radar.log import setup_logging
from radar.models import Analysis, RawItem
from radar.pipeline.analyze import AnalyzeStats
from radar.pipeline.collect import SourceResult
from radar.pipeline.cycle import CycleReport, run_cycle
from radar.pipeline.dedup import DedupStats
from radar.pipeline.prefilter import PrefilterStats
from radar.prompts import ANALYZE_PROMPT_VERSION, DEDUP_PROMPT_VERSION
from radar.services import build_services
from radar.sources import load_sources, sync_sources


def sync_sources_from_file() -> None:
    settings = get_settings()
    loaded = load_sources(settings.sources_file)
    for line in loaded.errors:
        print(f"ОШИБКА в {settings.sources_file}: {line}")
    with session_scope() as session:
        synced = sync_sources(session, loaded.sources)
    print(
        f"Источники: в файле {len(loaded.sources)}, добавлено {synced.created}, "
        f"обновлено {synced.updated}, ошибок {len(loaded.errors)}"
    )


def _print_collect(results: list[SourceResult]) -> None:
    print("\n=== СБОР ===")
    for result in results:
        if result.error:
            status = f"ОШИБКА: {result.error}"
        elif result.skipped_reason:
            status = f"пропущен: {result.skipped_reason}"
        else:
            status = (
                f"получено {result.fetched}, новых {result.new}, "
                f"старых {result.old}, уже в базе {result.duplicates}"
            )
        print(f"  {result.source_name:<40} {status}")

    new_ids = [item_id for result in results for item_id in result.new_item_ids]
    failed = sum(1 for result in results if result.error)
    print(f"\nИтого: источников {len(results)}, с ошибкой {failed}, новых материалов {len(new_ids)}")
    if not new_ids:
        return

    print("\n=== НОВЫЕ МАТЕРИАЛЫ ===")
    with session_scope() as session:
        rows = session.scalars(
            select(RawItem).where(RawItem.id.in_(new_ids)).order_by(RawItem.published_at.desc())
        ).all()
        for row in rows:
            published = f"{row.published_at:%d.%m %H:%M} UTC" if row.published_at else "без даты"
            preview = (row.title or row.text).replace("\n", " ")[:110]
            print(f"  [{row.id:>4}] {published} | {row.source.name} | {preview}")
            print(f"         {row.url}")


def _print_prefilter(stats: PrefilterStats) -> None:
    print("\n=== ПРЕДФИЛЬТР ===")
    reasons = ", ".join(f"{name}: {count}" for name, count in stats.reasons.items()) or "нет"
    print(f"  проверено {stats.checked}, прошло {stats.passed}, отсеяно — {reasons}")


def _print_analyze(stats: AnalyzeStats) -> None:
    print(f"\n=== LLM-АНАЛИЗ ({get_settings().llm_model}, промт {ANALYZE_PROMPT_VERSION}) ===")
    usage = stats.usage
    cost = f", стоимость {usage.cost:.4f}" if usage.cost else ""
    print(
        f"  проанализировано {stats.analyzed}, невалидный JSON {stats.invalid}, "
        f"сбой API {stats.failed}, отложено {stats.postponed}"
    )
    print(
        f"  запросов {usage.requests}, токены: вход {usage.prompt_tokens}, "
        f"выход {usage.completion_tokens}{cost}"
    )
    if not stats.analyzed_item_ids:
        return

    with session_scope() as session:
        rows = session.scalars(
            select(Analysis).where(Analysis.raw_item_id.in_(stats.analyzed_item_ids))
        ).all()
        rows = sorted(rows, key=lambda row: (not row.result["is_relevant"], -row.result["importance"]))
        relevant = sum(1 for row in rows if row.result["is_relevant"])
        print(f"  релевантных {relevant} из {len(rows)}\n")
        for row in rows:
            result = row.result
            mark = "+" if result["is_relevant"] else "-"
            check = " ⚠ проверить" if result["needs_verification"] else ""
            print(
                f"  {mark} [{result['importance']}/5] {result['geo_level']:<15} "
                f"{result['content_type']:<12} {result['urgency']:<10} {result['headline']}{check}"
            )
            if result["is_relevant"]:
                print(f"      {result['summary']}")
                print(f"      #{result['topic']} · {row.raw_item.source.name} · {row.raw_item.url}")


def _print_dedup(stats: DedupStats) -> None:
    print(f"\n=== СОБЫТИЯ (дедупликация, промт {DEDUP_PROMPT_VERSION}) ===")
    cost = f", стоимость {stats.usage.cost:.4f}" if stats.usage.cost else ""
    print(
        f"  новых событий {stats.new_events}, дублей {stats.duplicates}, "
        f"обновлений {stats.updates}, сбоев {stats.failed}"
    )
    print(
        f"  запросов к LLM {stats.usage.requests}, токены: вход {stats.usage.prompt_tokens}, "
        f"выход {stats.usage.completion_tokens}{cost}"
    )


def print_report(report: CycleReport) -> None:
    _print_collect(report.collect)
    if report.prefilter:
        _print_prefilter(report.prefilter)
    if report.analyze:
        _print_analyze(report.analyze)
    if report.dedup:
        _print_dedup(report.dedup)
    if report.notify:
        stats = report.notify
        skipped = ", ".join(f"{name}: {count}" for name, count in stats.skipped.items()) or "нет"
        print("\n=== УВЕДОМЛЕНИЯ ===")
        print(
            f"  отправлено {stats.sent}, обновлений {stats.updates}, в сводке {stats.in_digest}, "
            f"ошибок {stats.failed}; не отправлялось — {skipped}"
        )
    titles = {"analyze": "LLM-АНАЛИЗ", "dedup": "СОБЫТИЯ", "notify": "УВЕДОМЛЕНИЯ"}
    for step, reason in report.skipped_steps.items():
        print(f"\n=== {titles.get(step, step)} ===\n  шаг пропущен: {reason}")
    if report.error:
        print(f"\nЦИКЛ ПРЕРВАН ОШИБКОЙ: {report.error}")
    print(f"\nЦикл занял {report.duration_seconds} с")


async def run() -> None:
    sync_sources_from_file()
    services = build_services()
    try:
        report = await run_cycle(
            services.client, services.embedder, services.sender,
            skip_reasons=services.skip_reasons,
        )
    finally:
        await services.close()
    print_report(report)


def main() -> None:
    setup_logging(get_settings().log_level)
    init_db()
    asyncio.run(run())


if __name__ == "__main__":
    main()
