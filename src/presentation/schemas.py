# src/presentation/schemas.py
"""Строгие входные контракты OpenAPI; исходная пятёрка дополнительно проверяется в domain."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class SubmitMapping(BaseModel):
    """Запрос основной интеграции по UUID готовящегося извлечения."""

    model_config = ConfigDict(extra="forbid")
    source_job_id: UUID


class SourceRow(BaseModel):
    """Пять исходных полей: типы не приводятся и пробелы не нормализуются."""

    model_config = ConfigDict(extra="forbid")
    Caption: StrictStr
    Units: StrictStr
    Quantity: StrictStr
    UserComment1: StrictStr
    Pages: StrictStr


class SourceDocument(BaseModel):
    """Готовый документ для независимого JSON-импорта с сохранением порядка и дублей."""

    model_config = ConfigDict(extra="forbid")
    items: list[SourceRow]


class Selection(BaseModel):
    """Явный ручной выбор или отказ с защитой от перезаписи устаревшей вкладкой."""

    model_config = ConfigDict(extra="forbid")
    catalog_item_guid: UUID | None
    revision: int = Field(ge=0)


class MappingStatus(BaseModel):
    """Публичное состояние без lease, owner и confidential строк исходного документа."""

    job_id: UUID
    kind: Literal["mapping", "catalog"]
    status: str
    stage: str
    source_job_id: UUID | None
    created_at: float
    expires_at: float
    revision: int
    error_code: str | None
    total: int
    processed: int
    counts: dict[str, int]
    catalog_version: str | None
    links: dict[str, str]
