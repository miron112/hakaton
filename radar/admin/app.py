"""Админка радара. Запуск: streamlit run radar/admin/app.py"""

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# streamlit запускает файл как скрипт: корень проекта нужно добавить в путь импорта
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from radar.admin import queries  # noqa: E402
from radar.bot.notifier import ACTION_LABELS, GEO_LABELS  # noqa: E402
from radar.config import get_settings  # noqa: E402
from radar.db import init_db, session_scope  # noqa: E402
from radar.sources import SourceConfig, sync_sources  # noqa: E402
from radar.timeutil import LOCAL_TZ  # noqa: E402

REACTION_LABELS = {**ACTION_LABELS, queries.REACTION_NONE: "— без реакции"}

st.set_page_config(page_title="Новостной радар", page_icon="📡", layout="wide")
init_db()


def _local_date_to_utc(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def sources_tab() -> None:
    with session_scope() as session:
        rows = queries.sources_table(session)
    if not rows:
        st.info("Источников пока нет. Добавьте их в sources.yaml или формой ниже.")
    else:
        failing = sum(1 for row in rows if row["status"] == "ошибка")
        enabled = sum(1 for row in rows if row["enabled"])
        left, middle, right = st.columns(3)
        left.metric("Всего источников", len(rows))
        middle.metric("Включено", enabled)
        right.metric("С ошибкой", failing)

        frame = pd.DataFrame(rows)
        edited = st.data_editor(
            frame,
            hide_index=True,
            width="stretch",
            disabled=[column for column in frame.columns if column not in ("enabled", "priority")],
            column_config={
                "id": None,
                "enabled": st.column_config.CheckboxColumn("Вкл"),
                "priority": st.column_config.NumberColumn("Приоритет", min_value=1, max_value=3, step=1),
                "name": "Источник",
                "type": "Тип",
                "geo": "География",
                "role": "Роль",
                "address": "Адрес",
                "status": "Состояние",
                "last_success": "Последний успех",
                "errors_in_row": "Ошибок подряд",
                "last_error": "Последняя ошибка",
                "items": "Материалов",
            },
        )
        if st.button("Сохранить изменения", type="primary"):
            with session_scope() as session:
                changed = sum(
                    queries.update_source(session, int(row["id"]), bool(row["enabled"]), int(row["priority"]))
                    for row in edited.to_dict("records")
                )
            st.success(f"Изменено источников: {changed}")
            st.rerun()

    st.subheader("Добавить источник")
    with st.form("add_source", clear_on_submit=True):
        name = st.text_input("Название")
        left, middle, right = st.columns(3)
        source_type = left.selectbox("Тип", ["telegram", "rss", "html", "vk"])
        geo = middle.selectbox("География", ["surgut_city", "surgut_district", "khmao"])
        priority = right.selectbox("Приоритет", [1, 2, 3], index=1)
        url = st.text_input("Адрес ленты или страницы / имя канала / имя сообщества VK")
        note = st.text_input("Пометка для модели (необязательно)")
        st.caption("Только для типа html: CSS-селекторы")
        left, right = st.columns(2)
        list_selector = left.text_input("Блок одной новости", placeholder="div.news-item")
        link_selector = right.text_input("Ссылка внутри блока", placeholder="a.title")
        if st.form_submit_button("Добавить"):
            entry = {
                "name": name, "type": source_type, "url": url, "geo": geo,
                "priority": priority, "note": note or None,
            }
            if source_type == "html":
                entry["parser"] = {"list_selector": list_selector, "link_selector": link_selector}
            try:
                config = SourceConfig.model_validate(entry)
            except ValidationError as error:
                st.error("; ".join(err["msg"] for err in error.errors()))
            else:
                with session_scope() as session:
                    result = sync_sources(session, [config])
                st.success("Источник добавлен" if result.created else "Такой источник уже есть")
                st.rerun()


def history_tab() -> None:
    settings = get_settings()
    today = datetime.now(LOCAL_TZ).date()
    first, second, third, fourth, fifth = st.columns([2, 2, 2, 1, 2])
    period = first.date_input("Период", (today - timedelta(days=2), today))
    geo = second.multiselect("География", list(GEO_LABELS), format_func=GEO_LABELS.get)
    topics = third.multiselect("Тема", settings.topic_list)
    min_importance = fourth.number_input("Важность от", 1, 5, 1)
    reactions = fifth.multiselect("Реакция", list(REACTION_LABELS), format_func=REACTION_LABELS.get)
    if not isinstance(period, tuple) or len(period) != 2:
        st.info("Выберите начало и конец периода.")
        return

    with session_scope() as session:
        rows = queries.events_journal(
            session,
            since=_local_date_to_utc(period[0]),
            until=_local_date_to_utc(period[1] + timedelta(days=1)),
            geo=geo, topics=topics, min_importance=int(min_importance), reactions=reactions,
        )
    if not rows:
        st.info("За выбранный период событий нет.")
        return

    leads = [row["lead_minutes"] for row in rows if row["lead_minutes"] is not None]
    left, middle, right = st.columns(3)
    left.metric("Событий", len(rows))
    middle.metric("Есть публикация редакции", len(leads))
    right.metric("Радар нашёл раньше", sum(1 for lead in leads if lead > 0))

    frame = pd.DataFrame(rows).drop(columns=["summary", "key_facts", "links"])
    frame["reaction"] = frame["reaction"].map(REACTION_LABELS)
    frame["geo"] = frame["geo"].map(GEO_LABELS)
    st.dataframe(
        frame, hide_index=True, width="stretch",
        column_config={
            "id": "№", "found": "Найдено", "importance": "Важн.", "headline": "Событие",
            "geo": "География", "topic": "Тема", "first_source": "Первый источник",
            "published": "Опубликовано", "sources_count": "Источников",
            "notified": "Отправлено", "reaction": "Реакция",
            "own_published": "Публикация редакции", "lead_minutes": "Опережение, мин",
        },
    )
    st.caption(
        "Опережение — на сколько минут радар нашёл событие раньше публикации редакции "
        "(отрицательное — редакция написала раньше)."
    )

    st.subheader("Подробности и источники")
    for row in rows[:100]:
        with st.expander(f"[{row['importance']}/5] {row['headline']} — {row['found']}"):
            st.write(row["summary"])
            for fact in row["key_facts"]:
                st.markdown(f"- {fact}")
            for link in row["links"]:
                target = f"[{link['source']}]({link['url']})" if link["url"] else link["source"]
                st.markdown(f"**{link['relation']}** · {target} · {link['published']}")


def stats_tab() -> None:
    with session_scope() as session:
        daily = queries.daily_stats(session)
        quality = queries.source_quality(session)

    if daily:
        frame = pd.DataFrame(daily).set_index("day")
        today_row = frame.iloc[-1]
        decided = int(frame["take"].sum() + frame["skip"].sum())
        left, middle, right = st.columns(3)
        left.metric("Событий за последний день", int(today_row["events"]))
        middle.metric("Уведомлений за последний день", int(today_row["notifications"]))
        right.metric(
            "Доля «Берём» среди оценённых",
            f"{frame['take'].sum() / decided:.0%}" if decided else "нет оценок",
        )
        st.subheader("По дням")
        st.bar_chart(frame[["events", "notifications"]].rename(
            columns={"events": "События", "notifications": "Уведомления"}))
        st.dataframe(
            frame.rename(columns={
                "events": "События", "notifications": "Уведомления", "take": "👍 Берём",
                "skip": "👎 Мимо", "in_progress": "✅ Уже в работе",
            }),
            width="stretch",
        )
    else:
        st.info("Пока нет данных по дням.")

    st.subheader("Источники: польза и шум")
    if quality:
        frame = pd.DataFrame(quality).rename(columns={
            "source": "Источник", "role": "Роль", "analyzed": "Проанализировано",
            "relevant": "Релевантных", "events": "Событий (первым)", "take": "👍 Берём",
            "in_progress": "✅ Уже в работе", "skip": "👎 Мимо", "noise_share": "Доля шума",
        })
        st.dataframe(frame, hide_index=True, width="stretch")
        st.caption(
            "Доля шума — часть материалов источника, которую модель признала нерелевантной. "
            "Реакции считаются по событиям, которые источник сообщил первым."
        )


st.title("📡 Новостной радар Сургута и Югры")
tab_sources, tab_history, tab_stats = st.tabs(["Источники", "История находок", "Статистика"])
with tab_sources:
    sources_tab()
with tab_history:
    history_tab()
with tab_stats:
    stats_tab()
