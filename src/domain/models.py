# src/domain/models.py
"""Неизменяемые исходные строки и переносимые модели каталога и результатов."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from json import dumps
from typing import Any

SOURCE_FIELDS = ("Caption", "Units", "Quantity", "UserComment1", "Pages")
ALGORITHM_VERSION = "technical-baseline-v1"
TERMINAL = {"ready_for_review", "complete", "failed"}


class MapperError(Exception):
    """Передаёт безопасный код ожидаемой ошибки между слоями приложения."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        """Сохраняет публичную диагностику без исходного текста документов и credentials."""
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class CatalogItem:
    """Позиция снимка; guid внутренний, onec_guid заполнен только реальной ссылкой 1С."""

    guid: str
    code: str
    name: str
    unit: str
    onec_guid: str | None = None
    article: str = ""
    brand: str = ""
    manufacturer: str = ""
    group: str = ""
    active: bool = True

    def to_dict(self) -> dict:
        """Готовит независимое представление позиции для хранения и API."""
        return asdict(self)

    @property
    def content_hash(self) -> str:
        """Связывает вектор со всеми исходными характеристиками конкретной позиции."""
        return digest(self.to_dict())


@dataclass(frozen=True)
class Candidate:
    """Кандидат с честным cosine-score и отдельным детерминированным рангом."""

    item: CatalogItem
    score: float
    rank: float
    conflicts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Сериализует объяснение ранга без представления score вероятностью."""
        return asdict(self)


@dataclass
class ParsedCatalog:
    """Проверенный снимок XML и явная диагностика неоднозначных кодов."""

    items: list[CatalogItem]
    source_hash: str
    warnings: list[str] = field(default_factory=list)


def digest(value: Any) -> str:
    """Вычисляет воспроизводимый хеш структуры, сохраняя значения строк дословно."""
    return sha256(dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_source(payload: object, max_items: int = 10000) -> dict:
    """Отклоняет неполный контракт, числа вместо строк и лишние поля без исправлений."""
    if not isinstance(payload, dict) or set(payload) != {"items"}:
        raise MapperError("invalid_source", "Ожидается JSON с единственным полем items")
    items = payload["items"]
    if not isinstance(items, list) or len(items) > max_items:
        raise MapperError("invalid_source", "Недопустимое количество строк спецификации")
    for row in items:
        if not isinstance(row, dict) or set(row) != set(SOURCE_FIELDS):
            raise MapperError(
                "invalid_source", "Каждая строка должна содержать пять исходных полей"
            )
        if any(not isinstance(v, str) or len(v) > 32000 for v in row.values()):
            raise MapperError(
                "invalid_source", "Исходные поля должны быть строками до 32000 символов"
            )
    return payload
