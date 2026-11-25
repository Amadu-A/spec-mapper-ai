# src/presentation/lifecycle_schemas.py
"""Типизированный аддитивный контракт lifecycle, без source/financial/upstream identifiers."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PageProgress(BaseModel):
    """Только доказанные upstream состояния физической страницы, без raw VLM stream."""

    pdf_page: int
    state: str
    stage: str | None = None


class FileProgress(BaseModel):
    """Структура отображения источника; не означает завершённого извлечения при приёме файла."""

    file_index: int
    source_file: str | None = None
    page_count: int | None = None
    selected_pages: list[int] = Field(default_factory=list)
    pages: list[PageProgress] = Field(default_factory=list)


class LifecycleSnapshot(BaseModel):
    """Snapshot и event cursor независимы от CAS revision выбора/утверждения."""

    job_id: UUID
    profile: Literal["specification", "invoice"]
    api_root: str
    status: Literal[
        "queued",
        "extracting",
        "matching",
        "cancelling",
        "awaiting_review",
        "approved",
        "cancelled",
        "failed",
    ]
    stage: str
    revision: int
    catalog_version: str | None = None
    approved_at: float | None = None
    approved_revision: int | None = None
    created_at: float
    started_at: float | None
    updated_at: float
    expires_at: float
    elapsed_seconds: float
    wait_seconds: float
    last_event_id: int
    journal_start_id: int | None = None
    cancel_requested: bool
    error_code: str | None
    reused: bool = False
    upstream_status: str | None
    extracting_applicable: bool
    processed_pages: int
    total_pages: int | None
    current_file_index: int | None
    current_pdf_page: int | None
    files: list[FileProgress]
    processed_items: int
    total_items: int | None
    file_count: int | None
    document_count: int | None
    matched_count: int | None
    counts: dict[str, int]
    unresolved_count: int | None
    financial_warning_count: int | None
    links: dict[str, str]


class LifecyclePage(BaseModel):
    """Пагинированная история только текущего owner; не операторский доступ к документам."""

    total: int
    active_total: int
    offset: int
    items: list[LifecycleSnapshot]
