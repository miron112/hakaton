"""Проверка географии, выставленной моделью.

Модель склонна ставить «Югра» любой статье регионального портала — просто по названию
источника. У статей с сайтов (rss, html) текст полный, и местное событие почти всегда
называет Югру или её населённый пункт. Нет ни одного такого слова — местная география
не подтверждена. Для Telegram и VK правило не применяется: там в коротких постах место
часто подразумевается («на Ленина прорвало трубу»).
"""

from typing import Any

LOCAL_GEO_LEVELS = ("surgut_city", "surgut_district", "khmao")
ARTICLE_SOURCE_TYPES = ("rss", "html")

# Основы слов в нижнем регистре, «ё» заменена на «е»
LOCAL_MARKERS = (
    "югр", "югор", "хмао", "ханты-манс", "сургут",
    "нижневартовск", "нефтеюганск", "когалым", "нягань", "нягани", "мегион", "лангепас",
    "пыть-ях", "урай", "урае", "югорск", "покачи", "покачах", "радужн", "белоярск", "березов",
    "лянтор", "федоровск", "белый яр", "белом яре", "белого яра", "барсово", "нижнесортымск",
    "солнечн", "локосово", "русскинск", "сытомино", "тундрино", "угут", "ульт-ягун", "излучинск",
    "пойковск",
)


def has_local_marker(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(marker in lowered for marker in LOCAL_MARKERS)


def check_local_geo(data: dict[str, Any], source_type: str, text: str) -> bool:
    """Снимает неподтверждённую местную географию. Возвращает True, если результат изменён."""
    if source_type not in ARTICLE_SOURCE_TYPES or data["geo_level"] not in LOCAL_GEO_LEVELS:
        return False
    evidence = " ".join([text, *data.get("places", [])])
    if has_local_marker(evidence):
        return False
    data["geo_level"] = "unknown"
    data["affects_surgut"] = False
    data["confidence"] = min(data.get("confidence", 0.5), 0.3)
    return True
