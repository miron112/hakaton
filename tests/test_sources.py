"""Все адреса здесь — заглушки на example.com, не реальные источники."""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.models import Source
from radar.sources import load_sources, sync_sources

VALID_YAML = """
- name: "Тестовая лента"
  type: rss
  url: "https://example.com/rss.xml"
  geo: surgut_city
  priority: 1
- name: "Тестовый сайт"
  type: html
  url: "https://example.com/news"
  geo: khmao
  parser:
    list_selector: "div.news-item"
    link_selector: "a.title"
- name: "Тестовый канал"
  type: telegram
  url: "https://t.me/s/example_channel"
  geo: surgut_district
- name: "Тестовая стена"
  type: vk
  url: "https://vk.com/example_club"
  geo: surgut_city
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_sources_are_loaded_and_normalized(tmp_path: Path) -> None:
    result = load_sources(_write(tmp_path, VALID_YAML))

    assert result.errors == [] and result.skipped == []
    by_type = {source.type: source for source in result.sources}
    assert by_type["telegram"].url == "example_channel"
    assert by_type["vk"].url == "example_club"
    assert by_type["html"].parser is not None
    assert by_type["telegram"].priority == 2  # значение по умолчанию


def test_todo_placeholders_are_skipped(tmp_path: Path) -> None:
    text = """
- name: "TODO: Администрация Сургута"
  type: html
  url: "TODO"
  geo: surgut_city
  priority: 1
  parser:
    list_selector: "TODO"
    link_selector: "TODO"
"""
    result = load_sources(_write(tmp_path, text))

    assert result.sources == [] and result.errors == []
    assert len(result.skipped) == 1


def test_invalid_entry_does_not_block_others(tmp_path: Path) -> None:
    text = """
- name: "Сайт без селекторов"
  type: html
  url: "https://example.com/news"
  geo: surgut_city
- name: "Плохая география"
  type: rss
  url: "https://example.com/a.xml"
  geo: moscow
- name: "Плохой приоритет"
  type: rss
  url: "https://example.com/b.xml"
  geo: khmao
  priority: 7
- name: "Нормальная лента"
  type: rss
  url: "https://example.com/c.xml"
  geo: khmao
- name: "Дубль ленты"
  type: rss
  url: "https://example.com/c.xml"
  geo: khmao
"""
    result = load_sources(_write(tmp_path, text))

    assert [source.name for source in result.sources] == ["Нормальная лента"]
    assert len(result.errors) == 4


def test_handle_and_role_fields(tmp_path: Path, session: Session) -> None:
    text = """
- name: "Канал"
  type: telegram
  handle: Example_Channel        # комментарий
  geo: surgut_city
- name: "Своя лента"
  type: rss
  url: "https://example.com/own.xml"
  geo: surgut_city
  role: own
"""
    result = load_sources(_write(tmp_path, text))

    assert result.errors == []
    assert result.sources[0].url == "Example_Channel"
    assert [source.role for source in result.sources] == ["source", "own"]
    sync_sources(session, result.sources)
    assert session.scalar(select(Source.role).where(Source.type == "rss")) == "own"


def test_empty_file_is_ok(tmp_path: Path) -> None:
    assert load_sources(_write(tmp_path, "# пусто\n")).sources == []


def test_sync_is_idempotent_and_keeps_admin_changes(tmp_path: Path, session: Session) -> None:
    configs = load_sources(_write(tmp_path, VALID_YAML)).sources

    first = sync_sources(session, configs)
    assert (first.created, first.updated) == (4, 0)

    feed = session.scalar(select(Source).where(Source.type == "rss"))
    assert feed is not None
    feed.enabled = False  # редактор выключил источник в админке
    feed.priority = 3

    configs[0].name = "Переименованная лента"
    second = sync_sources(session, configs)

    assert (second.created, second.updated) == (0, 1)
    assert feed.name == "Переименованная лента"
    assert feed.enabled is False and feed.priority == 3
    assert len(session.scalars(select(Source)).all()) == 4
