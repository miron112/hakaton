"""Проверка Промта 1 на размеченном наборе: python -m radar.eval --file calibration.csv

Колонки CSV (разделитель — запятая или точка с запятой):
  text         — текст материала (обязательно)
  is_relevant  — разметка редакции: 1/0, да/нет, true/false (обязательно)
  importance   — разметка важности 1–5 (необязательно)
  title, source_name, source_type, url — необязательно
"""

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

from radar.config import get_settings
from radar.llm import LLMClient, LLMUsage, get_llm_client
from radar.log import setup_logging
from radar.pipeline.analyze import AnalysisResult, build_system_prompt, normalize
from radar.prompts import ANALYZE_PROMPT_VERSION, load_prompt, render

_TRUE = {"1", "true", "да", "yes", "y", "+", "взяли бы"}
_FALSE = {"0", "false", "нет", "no", "n", "-", "не взяли бы"}


@dataclass
class Row:
    number: int
    text: str
    title: str
    source_name: str
    source_type: str
    url: str
    expected_relevant: bool
    expected_importance: int | None


@dataclass
class Outcome:
    row: Row
    relevant: bool | None = None  # None — модель не дала валидный ответ
    importance: int | None = None
    headline: str = ""
    error: str = ""


@dataclass
class Metrics:
    total: int
    answered: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float | None
    recall: float | None
    importance_pairs: int
    importance_mae: float | None


def _parse_bool(value: str, number: int) -> bool:
    cleaned = value.strip().lower()
    if cleaned in _TRUE:
        return True
    if cleaned in _FALSE:
        return False
    raise ValueError(f"строка {number}: не понял is_relevant={value!r} (нужно 1/0 или да/нет)")


def load_rows(path: str | Path) -> list[Row]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    delimiter = ";" if raw.split("\n", 1)[0].count(";") > raw.split("\n", 1)[0].count(",") else ","
    reader = csv.DictReader(raw.splitlines(keepends=True), delimiter=delimiter)
    missing = {"text", "is_relevant"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"в файле нет обязательных колонок: {', '.join(sorted(missing))}")

    rows = []
    for number, record in enumerate(reader, start=2):
        text = (record.get("text") or "").strip()
        if not text:
            continue
        importance = (record.get("importance") or "").strip()
        rows.append(
            Row(
                number=number,
                text=text,
                title=(record.get("title") or "").strip(),
                source_name=(record.get("source_name") or "").strip() or "калибровочный набор",
                source_type=(record.get("source_type") or "").strip() or "unknown",
                url=(record.get("url") or "").strip() or "нет",
                expected_relevant=_parse_bool(record.get("is_relevant") or "", number),
                expected_importance=int(importance) if importance else None,
            )
        )
    return rows


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def compute_metrics(outcomes: list[Outcome]) -> Metrics:
    answered = [outcome for outcome in outcomes if outcome.relevant is not None]
    tp = sum(1 for o in answered if o.relevant and o.row.expected_relevant)
    fp = sum(1 for o in answered if o.relevant and not o.row.expected_relevant)
    fn = sum(1 for o in answered if not o.relevant and o.row.expected_relevant)
    tn = sum(1 for o in answered if not o.relevant and not o.row.expected_relevant)
    pairs = [
        abs(o.importance - o.row.expected_importance)
        for o in answered
        if o.importance is not None and o.row.expected_importance is not None
    ]
    return Metrics(
        total=len(outcomes),
        answered=len(answered),
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision=_safe_div(tp, tp + fp),
        recall=_safe_div(tp, tp + fn),
        importance_pairs=len(pairs),
        importance_mae=_safe_div(sum(pairs), len(pairs)),
    )


def _user_prompt(row: Row) -> str:
    text = f"{row.title}\n\n{row.text}" if row.title else row.text
    return render(
        load_prompt("analyze_user"),
        source_name=row.source_name,
        source_type=row.source_type,
        source_priority=2,
        source_note="калибровка",
        published_at="неизвестно",
        url=row.url,
        text=text[: get_settings().llm_max_text_chars],
    )


async def evaluate(rows: list[Row], client: LLMClient) -> tuple[list[Outcome], LLMUsage]:
    settings = get_settings()
    system_prompt = build_system_prompt(settings)
    semaphore = asyncio.Semaphore(settings.llm_concurrency)
    usage = LLMUsage()

    async def run(row: Row) -> Outcome:
        try:
            async with semaphore:
                result = await client.complete_json(system_prompt, _user_prompt(row), AnalysisResult)
        except Exception as error:
            return Outcome(row, error=f"{type(error).__name__}: {error}"[:200])
        usage.requests += result.usage.requests
        usage.prompt_tokens += result.usage.prompt_tokens
        usage.completion_tokens += result.usage.completion_tokens
        usage.cost += result.usage.cost
        data = normalize(result.data, settings)
        return Outcome(row, data["is_relevant"], data["importance"], data["headline"])

    return list(await asyncio.gather(*(run(row) for row in rows))), usage


def _percent(value: float | None) -> str:
    return "н/д" if value is None else f"{value:.0%}"


def print_report(outcomes: list[Outcome], usage: LLMUsage, model: str) -> None:
    metrics = compute_metrics(outcomes)
    print(f"\nМодель {model}, промт {ANALYZE_PROMPT_VERSION}")
    print(f"Материалов: {metrics.total}, получен ответ: {metrics.answered}")
    print("\nis_relevant:")
    print(f"  precision {_percent(metrics.precision)} — доля нужного среди того, что модель показала бы")
    print(f"  recall    {_percent(metrics.recall)} — доля нужного, которую модель не пропустила")
    print(
        f"  верно показано {metrics.true_positive}, лишнее {metrics.false_positive}, "
        f"пропущено {metrics.false_negative}, верно отсеяно {metrics.true_negative}"
    )
    mae = "н/д" if metrics.importance_mae is None else f"{metrics.importance_mae:.2f}"
    print(f"\nimportance: средняя ошибка {mae} балла (по {metrics.importance_pairs} материалам)")
    print(f"\nТокены: вход {usage.prompt_tokens}, выход {usage.completion_tokens}, стоимость {usage.cost:.4f}")

    def show(title: str, selected: list[Outcome]) -> None:
        if not selected:
            return
        print(f"\n{title}:")
        for outcome in selected:
            preview = (outcome.row.title or outcome.row.text).replace("\n", " ")[:90]
            print(f"  строка {outcome.row.number}: [{outcome.importance}/5] {preview}")

    show("ПРОПУЩЕНО (редакция взяла бы, модель отсеяла)",
         [o for o in outcomes if o.relevant is False and o.row.expected_relevant])
    show("ЛИШНЕЕ (редакция не взяла бы, модель показала)",
         [o for o in outcomes if o.relevant and not o.row.expected_relevant])
    for outcome in outcomes:
        if outcome.error:
            print(f"  строка {outcome.row.number}: нет ответа — {outcome.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка Промта 1 на размеченном наборе")
    parser.add_argument("--file", required=True, help="CSV с разметкой редакции")
    parser.add_argument("--limit", type=int, default=None, help="взять только первые N строк")
    args = parser.parse_args()

    setup_logging("WARNING")
    try:
        rows = load_rows(args.file)[: args.limit]
        client = get_llm_client()
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(f"Ошибка: {error}")
    if not rows:
        sys.exit("В файле нет строк с текстом")
    outcomes, usage = asyncio.run(evaluate(rows, client))
    print_report(outcomes, usage, client.model)


if __name__ == "__main__":
    main()
