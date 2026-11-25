# src/presentation/invoice_schemas.py
"""OpenAPI invoice-профиля: отдельный versioned source и типизированный результат для 1С."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.domain.invoices.models import (
    Contract,
    InvoiceDocument,
    InvoiceFile,
    InvoiceItem,
    Summary,
)
from src.presentation.schemas import CandidatePage, CandidateResult, CatalogPosition

RowStatus = Literal["matched", "review", "not_found", "manual", "not_applicable"]


class InvoiceCandidates(CandidatePage):
    """Общие Top-5 с pinned версией каталога и алгоритма конкретного invoice job."""

    catalog_version: str
    algorithm_version: str


class InvoiceStatus(BaseModel):
    """Безопасный статус одного внешнего задания на 8087."""

    job_id: UUID
    status: Literal[
        "queued",
        "extracting",
        "matching",
        "awaiting_review",
        "approved",
        "failed",
        "cancelling",
        "cancelled",
    ]
    revision: int
    created_at: float
    expires_at: float
    approved_at: float | None
    error_code: str | None
    file_count: int
    document_count: int
    total: int
    processed: int
    unresolved_count: int
    financial_warning_count: int
    upstream_warning: bool
    preview_available: bool
    counts: dict[str, int]
    catalog_version: str | None
    links: dict[str, str]
    reused: bool = False
    progress: dict | None = None


class FinancialWarning(BaseModel):
    """Адрес финансового риска и безопасные коды; цифры остаются в исходном документе."""

    document_id: str
    file_index: int
    document_index: int
    validation_status: str
    codes: list[str]
    requires_ack: bool


class Reference(BaseModel):
    """Обратный указатель глобального row_index на исходную строку и физические страницы."""

    file_index: int
    document_index: int
    row_number: int
    source_pages: list[int]
    source_sha256: str


class InvoiceRow(BaseModel):
    """Исходная позиция с независимыми кандидатами/выбором, без финансовых patches."""

    index: int
    source: InvoiceItem
    selectable: bool
    reference: Reference
    file: dict
    document: dict
    selection: CatalogPosition | None
    status: RowStatus
    confirmed: bool
    manually_reviewed: bool
    candidates: list[CandidateResult]
    row_kind: str
    warnings: list[str]


class InvoiceRows(InvoiceStatus):
    """Страница счёта с общей revision и финансовыми предупреждениями по документам."""

    items: list[InvoiceRow]
    offset: int
    filtered_total: int
    source_hash: str
    algorithm_version: str
    financial_warnings: list[FinancialWarning]


class InvoiceApproval(BaseModel):
    """Явное согласие на обработку финансовых расхождений без их исправления."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    acknowledged_document_warnings: list[str] = Field(default_factory=list, max_length=20000)


class InvoiceMatching(BaseModel):
    """Оценка и признаки human review; score не является вероятностью правильности."""

    row_index: int
    status: RowStatus
    confirmed: bool
    manually_reviewed: bool
    approved: bool
    catalog_version: str
    score: float | None
    warnings: list[str]


class MappedItem(InvoiceItem):
    """Оригинальная позиция дополнена ровно одним onec и отдельным matching."""

    onec: CatalogPosition | None
    matching: InvoiceMatching


class MappedDocument(InvoiceDocument):
    """Документ сохраняет реквизиты/финансы и сообщает готовность реальных ссылок 1С."""

    items: list[MappedItem]
    document_index: int
    matching_summary: dict[str, int]
    ready_for_onec: bool


class MappedFile(InvoiceFile):
    """Порядок и метаданные исходного файла неизменны."""

    documents: list[MappedDocument]


class InvoiceApprovalInfo(Contract):
    """Финансовое согласие, время и владелец утверждения без секретных значений ключей."""

    acknowledged_document_warnings: list[str] = Field(default_factory=list)
    financial_warnings: list[FinancialWarning] = Field(default_factory=list)
    approved_at: float | None = None
    approved_revision: int | None = None
    owner: str | None = None
    catalog_version: str | None = None


class InvoiceMapping(Contract):
    """Полный invoice_mapping.v1; все исходные уровни сохраняются рядом с matching."""

    schema_version: Literal["invoice_mapping.v1"]
    job_id: str
    status: Literal["awaiting_review", "approved"]
    revision: int
    approved_at: float | None
    source_hash: str
    catalog_version: str
    algorithm_version: str
    summary: Summary
    files: list[MappedFile]
    approval: InvoiceApprovalInfo


class ApprovedInvoice(InvoiceMapping):
    """Итоговый immutable контракт; до его фиксации endpoint возвращает 409."""

    status: Literal["approved"]
    approved_at: float
