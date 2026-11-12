# src/domain/models.py
"""Неизменяемые исходные строки и переносимые модели каталога и результатов."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from json import dumps
from typing import Any

LEGACY_SOURCE_FIELDS = ("Caption", "Units", "Quantity", "UserComment1", "Pages")
SOURCE_FIELDS = (*LEGACY_SOURCE_FIELDS, "Brand", "Article", "Note")
ALGORITHM_VERSION = "technical-baseline-v3-explicit-attributes"
TERMINAL = {"ready_for_review", "complete", "failed", "approved"}


def ensure_approvable(state: str, rows: list[dict]) -> None:
    """Разрешает явное общее подтверждение текущих выборов после завершения подбора."""
    if state not in {"complete", "ready_for_review"}:
        raise MapperError("approval_not_ready", "Сопоставление ещё не завершено", 409)


def confirm_rows(rows: list[dict]) -> list[dict]:
    """Подтверждает текущие предложения и null, не выбирая нового кандидата за пользователя."""
    confirmed = deepcopy(rows)
    for row in confirmed:
        if row["status"] == "review":
            row["status"] = "manual" if row["selection"] else "not_found"
        row.update(confirmed=True, manually_reviewed=True)
    return confirmed


class MapperError(Exception):
    """Передаёт безопасный код ожидаемой ошибки между слоями приложения."""

    def __init__(
        self, code: str, message: str, status: int = 422, retry_after: int | None = None
    ) -> None:
        """Сохраняет публичную диагностику без исходного текста документов и credentials."""
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retry_after = retry_after


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
    short_name: str = ""
    full_name: str = ""

    def to_dict(self) -> dict:
        """Готовит независимое представление позиции для хранения и API."""
        return asdict(self)

    @property
    def content_hash(self) -> str:
        """Определяет изменения метаданных независимо от embedding fingerprint."""
        return digest(self.to_dict())

    @property
    def legacy_content_hash(self) -> str:
        """Находит старый кэш V1–V3 без добавленных в V4 полей."""
        return digest(
            {k: v for k, v in self.to_dict().items() if k not in {"short_name", "full_name"}}
        )


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
    """Проверенный снимок каталога и явная диагностика неоднозначных кодов."""

    items: list[CatalogItem]
    source_hash: str
    warnings: list[str] = field(default_factory=list)
    statistics: dict = field(default_factory=dict)
    records: list[CatalogItem] = field(default_factory=list)
    context: dict = field(default_factory=dict)


def active_catalog(
    records: list[CatalogItem], duplicate_policy="quarantine"
) -> tuple[list, list, dict]:
    """Исключает все неоднозначные коды и deleted одинаково для full и webhook batch."""
    counts = Counter(item.code for item in records)
    conflicts = sorted(code for code, count in counts.items() if count > 1)
    if conflicts and duplicate_policy == "reject":
        raise MapperError("duplicate_code", "Каталог содержит конфликтующие коды номенклатуры")
    active = [item for item in records if item.active and item.code not in conflicts]
    warnings = (
        ["Карантин: исключены все записи с кодами " + ", ".join(conflicts)] if conflicts else []
    )
    return (
        active,
        warnings,
        {
            "total_rows": len(records),
            "deleted_rows": sum(not item.active for item in records),
            "conflicting_codes": conflicts,
            "quarantined_rows": sum(counts[c] for c in conflicts),
            "active_count": len(active),
            "technical_repeats": 0,
        },
    )


def digest(value: Any) -> str:
    """Вычисляет воспроизводимый хеш структуры, сохраняя значения строк дословно."""
    return sha256(dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_source(payload: object, max_items: int = 10000) -> dict:
    """Отклоняет неполный контракт, числа вместо строк и лишние поля без исправлений."""
    if not isinstance(payload, dict) or set(payload) not in ({"items"}, {"file_name", "items"}):
        raise MapperError(
            "invalid_source", "Ожидается JSON с единственным items или file_name и items"
        )
    extended = "file_name" in payload
    if extended and (not isinstance(payload["file_name"], str) or len(payload["file_name"]) > 180):
        raise MapperError("invalid_source", "file_name должен быть строкой до 180 символов")
    items = payload["items"]
    if not isinstance(items, list) or len(items) > max_items:
        raise MapperError("invalid_source", "Недопустимое количество строк спецификации")
    for row in items:
        expected = SOURCE_FIELDS if extended else LEGACY_SOURCE_FIELDS
        if not isinstance(row, dict) or set(row) != set(expected):
            raise MapperError(
                "invalid_source", "Нужен полный набор исходных полей выбранной версии документа"
            )
        if any(not isinstance(v, str) or len(v) > 32000 for v in row.values()):
            raise MapperError(
                "invalid_source", "Исходные поля должны быть строками до 32000 символов"
            )
        if extended and any(
            len(row[key]) > limit
            for key, limit in (("Brand", 2000), ("Article", 4000), ("Note", 20000))
        ):
            raise MapperError("invalid_source", "Превышена длина Brand/Article/Note")
    return payload


def source_view(row: dict) -> dict:
    """Дополняет только производное представление legacy, не изменяя сохранённый оригинал."""
    return {key: row.get(key, "") for key in SOURCE_FIELDS}
