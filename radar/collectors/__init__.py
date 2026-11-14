from radar.collectors.base import CollectedItem, Collector
from radar.collectors.html import HtmlCollector
from radar.collectors.rss import RssCollector
from radar.collectors.telegram_web import TelegramWebCollector
from radar.collectors.vk import VkCollector

COLLECTORS: dict[str, type[Collector]] = {
    "rss": RssCollector,
    "html": HtmlCollector,
    "telegram": TelegramWebCollector,
    "vk": VkCollector,
}

__all__ = ["COLLECTORS", "CollectedItem", "Collector"]
