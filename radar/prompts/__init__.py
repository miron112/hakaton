"""Тексты промтов. При правке текста меняйте версию."""

from functools import cache
from pathlib import Path

ANALYZE_PROMPT_VERSION = "p1-v0.2"
DEDUP_PROMPT_VERSION = "p2-v0.2"

_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


def render(template: str, **variables: object) -> str:
    """Подставляет {{name}}. Значения вставляются как есть, без повторной подстановки."""
    parts = template.split("{{")
    result = [parts[0]]
    for part in parts[1:]:
        name, closed, rest = part.partition("}}")
        if not closed or name not in variables:
            raise KeyError(f"в промте не заполнена переменная {name.strip()[:40]!r}")
        result.append(str(variables[name]))
        result.append(rest)
    return "".join(result)
