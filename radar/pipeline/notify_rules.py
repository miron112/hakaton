"""Правила отправки уведомлений. Пороги — в конфиге."""

from dataclasses import dataclass
from typing import Any

from radar.config import Settings

MARK_BREAKING = "🔴"
MARK_IMPORTANT = "🟠"
MARK_NORMAL = "🟡"


@dataclass(frozen=True)
class NotifyDecision:
    send: bool
    marker: str
    reason: str


def marker_for(analysis: dict[str, Any], settings: Settings) -> str:
    importance = analysis["importance"]
    if analysis.get("urgency") == "breaking" and importance >= settings.breaking_min_importance:
        return MARK_BREAKING
    if importance >= settings.breaking_min_importance:
        return MARK_IMPORTANT
    return MARK_NORMAL


def decide(analysis: dict[str, Any], settings: Settings) -> NotifyDecision:
    """Решает, показывать ли редактору материал с таким результатом Промта 1."""
    marker = marker_for(analysis, settings)
    if not analysis["is_relevant"]:
        return NotifyDecision(False, marker, "not_relevant")

    geo = analysis["geo_level"]
    if settings.notify_local_only and geo in ("other", "unknown") and not analysis.get("affects_surgut"):
        return NotifyDecision(False, marker, "not_local")

    threshold = (
        settings.notify_min_importance_surgut
        if geo == "surgut_city"
        else settings.notify_min_importance
    )
    if analysis["importance"] < threshold:
        return NotifyDecision(False, marker, "below_threshold")
    return NotifyDecision(True, marker, "ok")
