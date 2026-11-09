# src/presentation/dashboard_schemas.py
"""Дополнительные OpenAPI DTO управления; существующие mapper DTO остаются совместимыми."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from src.presentation.schemas import MappingStatus


class SyncConfiguration(BaseModel):
    """Открытые поля GUI: пароль/token не принимаются, используется server credential reference."""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    enabled: StrictBool
    time_utc: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    source_type: Literal["local_xml", "http_onec"]
    url: str = Field("", max_length=2000)
    method: Literal["GET"] = "GET"
    auth_type: Literal["none", "bearer", "basic"] = "none"
    credential_ref: Literal["onec_catalog"] = "onec_catalog"
    expected_format: Literal["xml"] = "xml"
    export_mode: Literal["full"] = "full"
    timeout_seconds: int = Field(60, ge=1, le=300)
    max_bytes: int = Field(200000000, ge=1024, le=2000000000)
    completion_header: str = Field("", max_length=64)
    completion_value: str = Field("", max_length=256)


class ImportSource(BaseModel):
    """Опциональное тело прежнего catalog/imports; без тела остаётся local XML."""

    model_config = ConfigDict(extra="forbid")
    source_type: Literal["local_xml", "http_onec"]


class OperatorLogin(BaseModel):
    """Одноразовый код из SSH CLI; постоянный административный ключ сюда не вводится."""

    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(min_length=32, max_length=128)


class RefreshRequest(BaseModel):
    """Пользователь запускает сохранённый источник без возможности подменить его параметры."""

    model_config = ConfigDict(extra="forbid")


class RefreshAvailability(BaseModel):
    """Общий минутный интервал и безопасный прогресс, доступные обычной сессии."""

    available: bool
    reason: str | None
    source_type: str
    retry_after: int
    interval_seconds: int
    active: dict | None


class CatalogImportStatus(MappingStatus):
    """Расширенный прогресс каталога, не меняющий ответы mapping endpoints."""

    source_type: str = "local_xml"
    downloaded_bytes: int = 0
    metrics: dict = Field(default_factory=dict)


class SyncSettingsView(SyncConfiguration):
    """Публичный management DTO исключает secret values и private source identity."""

    updated_at: float
    next_run_at: float | None
    is_configured: bool
    secret_configured: bool
    contract_confirmed: bool
    probe: dict
    probe_current: bool
    configuration_error: str | None


class JobMetadata(MappingStatus):
    """Метаданные без source/result; can_open не заменяет серверную owner-проверку."""

    source_type: str
    can_open: bool


class JobsPage(BaseModel):
    """Пагинированный собственный или операторский обзор."""

    total: int
    offset: int
    items: list[JobMetadata]


class CatalogRun(BaseModel):
    """Технический отчёт без XML, наименований, owner и credentials."""

    id: str
    job_id: str
    source_type: str
    initiator: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    state: str
    stage: str
    details: dict
    events: list[dict]


class ImportHistoryPage(BaseModel):
    """Долговечная история импортов с offset/limit фильтрами."""

    total: int
    offset: int
    items: list[CatalogRun]
