# src/domain/invoices/models.py
"""Versioned DTO InvoiceExport 8083 без нормализации исходных финансов и реквизитов.

Контракт сверён с Extracting 9f4c911. Совместимые дополнительные поля сохраняются;
новая версия схемы требует явного изменения контракта. DTO проверяет вход, но его
model_dump не заменяет оригинальный JSON: отсутствующие поля остаются отсутствующими.
"""

from __future__ import annotations

import json
from datetime import date as calendar_date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.domain.models import MapperError

Text = Annotated[str, Field(max_length=20000)]
Code = Annotated[str, Field(max_length=1000)]
Number = Annotated[str, Field(pattern=r"^-?\d{1,24}(?:\.\d{1,12})?$", max_length=38)]
Pages = Annotated[list[Annotated[int, Field(ge=1)]], Field(max_length=500)]


class Contract(BaseModel):
    """Строгие типы и lossless расширения текущей версии без неявных преобразований."""

    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)


class Party(Contract):
    """Реквизиты стороны сохраняют ведущие нули и исходные пустые значения."""

    name: Text | None = None
    inn: Code | None = None
    kpp: Code | None = None


class InvoiceItem(Contract):
    """Все опубликованные поля позиции; деньги всегда decimal-строки либо null."""

    row_number: int = Field(ge=1)
    caption: Text = Field(min_length=1)
    brand: Code | None = None
    manufacturer: Code | None = None
    article: Code | None = None
    article_candidate: Code | None = None
    supplier_code: Code | None = None
    product_reference_raw: Text | None = None
    row_kind: Code = "product"
    units: Code | None = None
    quantity: Number | None = None
    unit_price_without_vat: Number | None = None
    unit_price_with_vat: Number | None = None
    line_total_without_vat: Number | None = None
    vat_rate: Number | None = None
    vat_amount: Number | None = None
    line_total_with_vat: Number | None = None
    source_pages: Pages = Field(default_factory=list)
    vat_rate_source: Literal["row", "document_total"] | None = None
    article_candidate_source: Text | None = None
    technical_parameters_pages: Pages = Field(default_factory=list)
    requires_manual_review: bool = False
    marked_with_asterisk: bool = False
    availability_note: Text | None = None
    order_multiplicity: Number | None = None
    field_origin: dict[str, Literal["source", "derived", "document_total"]] = Field(
        default_factory=dict
    )
    derived_fields: list[Code] = Field(default_factory=list)


class Adjustment(Contract):
    """Явная корректировка из документа, без повторного применения к итогам."""

    kind: Literal["discount", "delivery", "advance", "other"]
    without_vat: Number | None = None
    vat: Number | None = None
    with_vat: Number | None = None
    included_in_line_totals: bool


class Totals(Contract):
    """Неизменяемые итоги документа, независимые от заполнения отдельных строк."""

    without_vat: Number | None = None
    vat: Number | None = None
    with_vat: Number | None = None
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=100)


class Check(Contract):
    """Доказательство проверки 8083; Mapper не исправляет его статус и значения."""

    check: str
    status: Literal["passed", "failed", "not_checkable"]
    calculated: str | None = None
    document_value: str | None = None
    difference: str | None = None
    tolerance: str | None = None
    row_number: int | None = None


class Validation(Contract):
    """Финансовая валидация сохраняется отдельно от состояния сопоставления."""

    status: Literal["passed", "partially_checked", "failed", "not_checkable"] = "not_checkable"
    checks: list[Check] = Field(default_factory=list)
    warnings: list[Text] = Field(default_factory=list)


class InvoiceDocument(Contract):
    """Отдельный документ/поставщик; соседние документы и позиции не объединяются."""

    document_type: Literal["invoice", "commercial_offer", "specification", "unknown"] = "unknown"
    number: Code | None = None
    date: Code | None = Field(None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    supplier: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    currency: Code | None = None
    items: list[InvoiceItem] = Field(default_factory=list, max_length=20000)
    totals: Totals = Field(default_factory=Totals)
    validation: Validation = Field(default_factory=Validation)
    source_pages: Pages = Field(default_factory=list)
    attached_pages: Pages = Field(default_factory=list)
    related_commercial_offer_number: Code | None = None
    payment_terms: Text | None = None
    contract_reference: Text | None = None
    specification_reference: Text | None = None
    # Сохранённый корпус 8083 содержит notes строкой; новые ответы содержат список.
    notes: Annotated[list[Text], Field(max_length=200)] | Text | None = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        """Проверяет реальную ISO дату без преобразования или исправления исходного значения."""
        if value is not None and calendar_date.fromisoformat(value).isoformat() != value:
            raise ValueError("Ожидается реальная дата ISO")
        return value


class InvoiceFile(Contract):
    """Проверяемые метаданные источника и его документы в исходном порядке."""

    file_index: int = Field(ge=0)
    source_file: Code
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_count: int = Field(ge=1, le=5000)
    documents: list[InvoiceDocument] = Field(default_factory=list, max_length=20000)


class Summary(Contract):
    """Исходные счётчики без суммирования денег разных поставщиков и валют."""

    file_count: int = Field(ge=0)
    document_count: int = Field(ge=0)
    item_count: int = Field(ge=0)


class InvoiceExport(Contract):
    """Полный публичный invoice_extraction.v1, принимаемый обоими входными адаптерами."""

    schema_version: Literal["invoice_extraction.v1"]
    files: list[InvoiceFile] = Field(min_length=1, max_length=10)
    summary: Summary


def validate_invoice(source: object, max_items: int = 20000) -> dict:
    """Проверяет версию, типы, границы/счётчики и зарезервированные поля без изменения входа."""
    try:
        json.dumps(source, allow_nan=False)
        parsed = InvoiceExport.model_validate(source)
        if {
            "job_id",
            "status",
            "revision",
            "approved_at",
            "source_hash",
            "catalog_version",
            "algorithm_version",
            "approval",
        } & parsed.model_extra.keys():
            raise ValueError("Зарезервированные поля")
        count = sum(len(doc.items) for file in parsed.files for doc in file.documents)
        documents = sum(len(file.documents) for file in parsed.files)
        if count > max_items or (
            parsed.summary.file_count,
            parsed.summary.document_count,
            parsed.summary.item_count,
        ) != (len(parsed.files), documents, count):
            raise ValueError("Счётчики не соответствуют документу")
        for index, file in enumerate(parsed.files):
            if file.file_index != index:
                raise ValueError("Нарушена адресация файлов")
            for doc in file.documents:
                if any(p > file.page_count for p in [*doc.source_pages, *doc.attached_pages]):
                    raise ValueError("Страница вне файла")
                if {
                    "document_index",
                    "matching_summary",
                    "ready_for_onec",
                } & doc.model_extra.keys():
                    raise ValueError("Зарезервированные поля")
                for item in doc.items:
                    if any(
                        p > file.page_count
                        for p in [*item.source_pages, *item.technical_parameters_pages]
                    ):
                        raise ValueError("Страница вне файла")
                    if {"onec", "matching", "mapping", "index"} & item.model_extra.keys():
                        raise ValueError("Зарезервированные поля")
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise MapperError(
            "invalid_invoice_source", "Некорректный полный документ invoice_extraction.v1", 422
        ) from exc
    return source
