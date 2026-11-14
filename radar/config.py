"""Настройки приложения. Все значения читаются из окружения / файла .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TOPICS = (
    "ЧП и происшествия, ЖКХ, транспорт и дороги, власть и политика, экономика и бизнес, "
    "строительство и благоустройство, здравоохранение, образование, экология, "
    "культура и спорт, общество, другое"
)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Хранилище и источники
    database_url: str = "sqlite:///data/radar.db"
    sources_file: str = "sources.yaml"

    # LLM
    llm_provider: str = "openai_compat"
    llm_base_url: str = "https://polza.ai/api/v1"
    llm_api_key: str | None = None
    llm_model: str = "openai/gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_timeout_seconds: float = 60.0
    llm_json_mode: bool = True  # выключить, если модель не поддерживает response_format
    llm_concurrency: int = 5
    llm_max_items_per_cycle: int = 200  # страховка бюджета; остальное уйдёт в следующий цикл
    llm_max_text_chars: int = 6000

    # Telegram / VK
    telegram_bot_token: str | None = None
    telegram_editor_chat_id: int | None = None
    # true — снять вебхук, если он стоит на боте (отключит бота от сервиса, который его поставил)
    telegram_delete_webhook: bool = False
    vk_token: str | None = None
    vk_api_version: str = "5.199"
    vk_posts_per_request: int = 20

    # Сбор
    poll_interval_minutes: int = 15
    fetch_timeout_seconds: float = 15.0
    fetch_retries: int = 2
    fetch_retry_pause_seconds: float = 2.0
    fetch_concurrency: int = 10
    user_agent: str = "Mozilla/5.0 (compatible; SurgutNewsRadar/0.1; hackathon prototype)"
    initial_lookback_hours: int = 24
    html_article_concurrency: int = 3  # одновременных загрузок статей с одного сайта

    # Предфильтр
    min_text_len: int = 40  # заголовок + текст; в RSS описания короткие
    max_age_hours: int = 48
    stop_words: str = ""

    # Промт 1: переменные редакции
    editorial_priorities: str = "Особых указаний нет."
    excluded_topics: str = "Федеральная повестка без связи с Югрой; реклама и розыгрыши."
    topics: str = DEFAULT_TOPICS

    # Дедупликация
    embedding_model: str = "intfloat/multilingual-e5-small"
    dedup_window_hours: int = 48
    # У e5 даже несвязанные новости дают ~0.80–0.89; одно событие — от ~0.93 (проверено на данных 14.11)
    dedup_sim_threshold: float = 0.90
    dedup_top_k: int = 5

    # Уведомления
    notify_min_importance: int = 3
    notify_min_importance_surgut: int = 2
    breaking_min_importance: int = 4
    max_notifications_per_cycle: int = 10
    # true — не слать события с geo other/unknown, если они не затрагивают Сургут
    notify_local_only: bool = False

    log_level: str = "INFO"

    @property
    def stop_word_list(self) -> list[str]:
        return [word.lower() for word in _split_csv(self.stop_words)]

    @property
    def topic_list(self) -> list[str]:
        return _split_csv(self.topics)


@lru_cache
def get_settings() -> Settings:
    return Settings()
