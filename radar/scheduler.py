"""Основной процесс: цикл каждые POLL_INTERVAL_MINUTES + бот. Запуск: python -m radar.scheduler"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from radar.bot.handlers import router
from radar.bot.telegram import prepare_polling
from radar.config import get_settings
from radar.db import init_db, session_scope
from radar.log import log_event, setup_logging
from radar.pipeline import cycle
from radar.services import Services, build_services
from radar.sources import load_sources, sync_sources

JOB_ID = "cycle"


def _resync_sources() -> None:
    """sources.yaml перечитывается каждый цикл: новый источник подхватится без перезапуска."""
    try:
        loaded = load_sources(get_settings().sources_file)
        with session_scope() as session:
            sync_sources(session, loaded.sources)
        for line in loaded.errors:
            log_event("sources", "invalid_entry", level=logging.WARNING, error=line)
    except Exception as error:
        log_event("sources", "error", level=logging.ERROR, error=f"{type(error).__name__}: {error}")


async def _job(services: Services, scheduler: AsyncIOScheduler) -> None:
    _resync_sources()
    await cycle.run_cycle(
        services.client, services.embedder, services.sender, skip_reasons=services.skip_reasons
    )
    job = scheduler.get_job(JOB_ID)
    if job and job.next_run_time:
        cycle.state.next_run_at = job.next_run_time.astimezone(timezone.utc).replace(tzinfo=None)


async def run() -> None:
    settings = get_settings()
    services = build_services()
    for step, reason in services.skip_reasons.items():
        log_event("startup", "step_disabled", level=logging.WARNING, step=step, reason=reason)

    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        _job,
        IntervalTrigger(minutes=settings.poll_interval_minutes),
        args=[services, scheduler],
        id=JOB_ID,
        next_run_time=datetime.now(timezone.utc),  # первый цикл сразу
        max_instances=1,  # затянувшийся цикл не наслаивается на следующий
        coalesce=True,
        misfire_grace_time=None,
    )
    scheduler.start()
    log_event("startup", "ok", interval_minutes=settings.poll_interval_minutes, bot=services.bot is not None)

    try:
        if services.bot is not None and await prepare_polling(services.bot, settings):
            dispatcher = Dispatcher()
            dispatcher.include_router(router)
            await dispatcher.start_polling(services.bot)
        else:
            await asyncio.Event().wait()  # кнопки и команды недоступны — просто крутим планировщик
    finally:
        scheduler.shutdown(wait=False)
        await services.close()


def main() -> None:
    setup_logging(get_settings().log_level)
    init_db()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
