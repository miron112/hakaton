"""Структурированные логи: время, этап, источник, результат + произвольные поля."""

import logging
import sys
from typing import Any

_configured = False


def setup_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    # Консоль Windows по умолчанию не в UTF-8 и падает на эмодзи и части кириллицы
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def _format_value(value: Any) -> str:
    text = str(value).replace("\n", " ")
    return f'"{text}"' if " " in text else text


def log_event(
    stage: str,
    result: str,
    source: str | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Пишет строку вида: stage=collect source="Канал" result=ok new=3."""
    pairs: dict[str, Any] = {"stage": stage}
    if source is not None:
        pairs["source"] = source
    pairs["result"] = result
    pairs.update(fields)
    message = " ".join(f"{key}={_format_value(value)}" for key, value in pairs.items())
    logging.getLogger("radar").log(level, message)
