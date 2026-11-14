"""Сборщик стен ВКонтакте через официальный API (wall.get)."""

import re
from datetime import datetime, timezone
from typing import Any

from radar.collectors.base import CollectedItem, Collector, clean_text
from radar.config import get_settings
from radar.models import Source

API_URL = "https://api.vk.com/method/wall.get"
_GROUP_ID = re.compile(r"^(?:club|public|event)(\d+)$")
_NUMERIC_ID = re.compile(r"^-?\d+$")


def wall_params(handle: str) -> dict[str, str]:
    """club123 / public123 → owner_id=-123; числовой id — как есть; иначе короткое имя."""
    group = _GROUP_ID.match(handle)
    if group:
        return {"owner_id": f"-{group.group(1)}"}
    if _NUMERIC_ID.match(handle):
        return {"owner_id": handle}
    return {"domain": handle}


def _post_text(post: dict[str, Any]) -> str:
    parts = [post.get("text") or ""]
    parts += [repost.get("text") or "" for repost in post.get("copy_history") or []]
    return clean_text("\n\n".join(part for part in parts if part.strip()))


def parse_wall(payload: dict[str, Any]) -> list[CollectedItem]:
    if "error" in payload:
        error = payload["error"]
        raise ValueError(f"VK API: {error.get('error_code')} {error.get('error_msg')}")

    items: list[CollectedItem] = []
    for post in payload.get("response", {}).get("items", []):
        text = _post_text(post)
        if not text or post.get("marked_as_ads"):
            continue
        post_key = f"{post['owner_id']}_{post['id']}"
        published = datetime.fromtimestamp(post["date"], tz=timezone.utc).replace(tzinfo=None)
        items.append(
            CollectedItem(
                external_id=post_key,
                url=f"https://vk.com/wall{post_key}",
                title=None,
                text=text,
                published_at=published,
            )
        )
    return items


class VkCollector(Collector):
    async def fetch(
        self, source: Source, known_ids: frozenset[str] = frozenset()
    ) -> list[CollectedItem]:
        settings = get_settings()
        if not settings.vk_token:
            raise RuntimeError("VK_TOKEN не задан в .env")
        # Токен передаём в теле запроса, а не в адресе: адреса попадают в логи
        response = await self.client.post(
            API_URL,
            data={
                **wall_params(source.url),
                "count": str(settings.vk_posts_per_request),
                "access_token": settings.vk_token,
                "v": settings.vk_api_version,
            },
        )
        response.raise_for_status()
        return parse_wall(response.json())
