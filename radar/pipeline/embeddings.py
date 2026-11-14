"""Локальные эмбеддинги для поиска похожих событий."""

from functools import lru_cache
from typing import Protocol

from radar.config import get_settings

E5_PREFIX = "query: "  # модели e5 обучены с префиксом; для симметричного сравнения — query


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Возвращает нормированные векторы (длина 1)."""


class E5Embedder:
    def __init__(self, model_name: str) -> None:
        # Импорт здесь: torch грузится несколько секунд и нужен только этому классу
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            [E5_PREFIX + text for text in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[round(float(value), 6) for value in vector] for vector in vectors]


@lru_cache
def get_embedder() -> Embedder:
    return E5Embedder(get_settings().embedding_model)


def cosine(left: list[float], right: list[float]) -> float:
    """Косинусная близость нормированных векторов."""
    return sum(a * b for a, b in zip(left, right))
