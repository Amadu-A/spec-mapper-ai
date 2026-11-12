# src/presentation/schemas.py
"""Версионные исходные контракты OpenAPI и отдельные схемы производных результатов."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class SubmitMapping(BaseModel):
    """Запрос основной интеграции по UUID готовящегося извлечения."""

    model_config = ConfigDict(extra="forbid")
    source_job_id: UUID


class LegacySourceRow(BaseModel):
    """Пять исходных строк legacy v1 без приведения типов и нормализации пробелов."""

    model_config = ConfigDict(extra="forbid", str_max_length=32000)
    Caption: StrictStr
    Units: StrictStr
    Quantity: StrictStr
    UserComment1: StrictStr
    Pages: StrictStr


class SourceRow(LegacySourceRow):
    """Производное представление дополняет legacy новыми пустыми реквизитами."""

    Brand: StrictStr = Field("", max_length=2000)
    Article: StrictStr = Field("", max_length=4000)
    Note: StrictStr = Field("", max_length=20000)


class ExtendedSourceRow(SourceRow):
    """Вход v2 требует явного присутствия всех восьми исходных реквизитов."""

    Brand: StrictStr = Field(max_length=2000)
    Article: StrictStr = Field(max_length=4000)
    Note: StrictStr = Field(max_length=20000)


class LegacySourceDocument(BaseModel):
    """Версия v1: только items с исходной пятёркой; OpenAPI запрещает смешанные версии."""

    model_config = ConfigDict(extra="forbid")
    items: list[LegacySourceRow] = Field(max_length=50000)


class ExtendedSourceDocument(BaseModel):
    """Версия v2: обязательное имя файла и восемь полей каждой строки."""

    model_config = ConfigDict(extra="forbid")
    file_name: StrictStr = Field(max_length=180)
    items: list[ExtendedSourceRow] = Field(max_length=50000)


SourceDocument = LegacySourceDocument | ExtendedSourceDocument


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
    approved_at: float | None = None
    approved_revision: int | None = None
    error_code: str | None
    total: int
    processed: int
    unresolved_count: int = 0
    counts: dict[str, int]
    catalog_version: str | None
    links: dict[str, str]


class Approval(BaseModel):
    """Ожидаемая версия целого результата для атомарного общего утверждения."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)


class CatalogPosition(BaseModel):
    """Публичная позиция: код строкой, внутренний UUID и отдельная реальная ссылка 1С."""

    guid: str
    code: str
    name: str
    unit: str
    onec_guid: str | None
    article: str
    brand: str
    manufacturer: str
    group: str
    active: bool
    short_name: str = ""
    full_name: str = ""
    catalog_version: str | None = None


class CandidateResult(BaseModel):
    """Кандидат Top-K с раздельными cosine, рангом и причинами ограничений."""

    item: CatalogPosition
    score: float
    rank: float
    conflicts: list[str]
    evidence: list[str]


class CandidatePage(BaseModel):
    """Кандидаты одной стабильной строки документа."""

    row_index: int
    candidates: list[CandidateResult]


class CatalogSearch(BaseModel):
    """Ограниченная страница поиска по всему активному каталогу."""

    items: list[CatalogPosition]


class MappingRow(BaseModel):
    """Исходная строка, независимый выбор и результаты автоматического подбора."""

    index: int
    source: SourceRow
    selection: CatalogPosition | None
    status: Literal["matched", "review", "not_found", "manual"]
    confirmed: bool
    manually_reviewed: bool
    candidates: list[CandidateResult]


class MappingResult(MappingStatus):
    """Готовая страница таблицы с totals всего задания и сохранённой revision."""

    filtered_total: int
    offset: int
    items: list[MappingRow]
    algorithm_version: str
    source_hash: str
    file_name: str = ""


class MatchingExport(BaseModel):
    """Признаки ревью и общего утверждения; score ручного выбора может отсутствовать."""

    status: Literal["matched", "review", "not_found", "manual"]
    confirmed: bool
    manually_reviewed: bool
    approved: bool
    score: float | None


class ExportRow(SourceRow):
    """Исходные реквизиты с индексом и отдельным объектом 1С либо явным null."""

    index: int
    onec: CatalogPosition | None
    matching: MatchingExport


class MappingExport(BaseModel):
    """Полный предварительный экспорт для клиента интеграции."""

    job_id: str
    file_name: str = ""
    source_job_id: str | None
    source_hash: str
    catalog_version: str
    algorithm_version: str
    status: str
    approved_at: float | None
    approved_revision: int | None
    items: list[ExportRow]


class ApprovalInfo(BaseModel):
    """Неизменяемые реквизиты общего утверждения; не содержат значение API-ключа."""

    approved_at: float
    approved_revision: int
    owner: str
    catalog_version: str


class ApprovedExport(MappingExport):
    """Контракт финального JSON, передаваемого в 1С после общего утверждения."""

    status: Literal["approved"]
    approved_at: float
    approved_revision: int
    approval: ApprovalInfo
