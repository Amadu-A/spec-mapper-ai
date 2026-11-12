# src/infrastructure/database.py
"""SQLAlchemy-модели постоянного каталога, кэша векторов и временных durable заданий."""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Единая metadata для Alembic и интеграционных проверок адаптеров."""


class JobRow(Base):
    """Атомарный агрегат задания; JSON source хранится отдельно от списка результатов."""

    __tablename__ = "mapping_jobs"
    __table_args__ = (UniqueConstraint("owner", "idempotency_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatched_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


class CatalogVersionRow(Base):
    """Неизменяемый снимок и метаданные совместимости его коллекции Qdrant."""

    __tablename__ = "catalog_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class ApprovalRow(Base):
    """Один неизменяемый утверждённый snapshot задания; удаляется только вместе с TTL задания."""

    __tablename__ = "mapping_approvals"
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mapping_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer)
    approved_at: Mapped[float] = mapped_column(Float)
    idempotency_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)


class CatalogEntryRow(Base):
    """Метаданные позиции конкретного снимка, устойчивые к последующей синхронизации."""

    __tablename__ = "catalog_entries"
    __table_args__ = (Index("ix_catalog_search", "version", "code"),)
    version: Mapped[str] = mapped_column(String(36), primary_key=True)
    guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(32000))
    article: Mapped[str] = mapped_column(String(32000))
    item_data: Mapped[dict] = mapped_column(JSON)


class CatalogStateRow(Base):
    """Единственный активный указатель, предыдущая версия и журнал последней попытки."""

    __tablename__ = "catalog_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[str | None] = mapped_column(String(36), nullable=True)
    previous: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_successful_sync: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    syncing: Mapped[bool] = mapped_column(Boolean, default=False)


class VectorCacheRow(Base):
    """Постоянный кэш по fingerprint модели и hash позиции: без повторного embedding."""

    __tablename__ = "catalog_vectors"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    vector: Mapped[list] = mapped_column(JSON)


class SyncSettingsRow(Base):
    """Постоянный singleton GUI-настроек; bootstrap env применяется только один раз."""

    __tablename__ = "catalog_sync_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    next_run_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=0)
    updated_by: Mapped[str] = mapped_column(String(80), default="bootstrap")
    probe_result: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_started_at: Mapped[float] = mapped_column(Float, default=0)


class SyncRunRow(Base):
    """Долговечный технический запуск; не зависит от TTL job и не содержит XML/наименования."""

    __tablename__ = "catalog_sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    owner: Mapped[str] = mapped_column(String(80))
    source_type: Mapped[str] = mapped_column(String(32))
    source_identity: Mapped[str] = mapped_column(String(64))
    initiator: Mapped[str] = mapped_column(String(32))
    schedule_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    events: Mapped[list] = mapped_column(JSON, default=list)


class DailyMetricRow(Base):
    """Дневные счётчики по хешу scope без job ID, текстов, кандидатов и иных документов."""

    __tablename__ = "dashboard_daily_metrics"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    jobs: Mapped[int] = mapped_column(Integer, default=0)
    positions: Mapped[int] = mapped_column(Integer, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    manual: Mapped[int] = mapped_column(Integer, default=0)
    review: Mapped[int] = mapped_column(Integer, default=0)
    unresolved: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)


class ServiceActivityRow(Base):
    """Последний heartbeat/успешный inbound запрос сервиса без значения credentials."""

    __tablename__ = "service_activity"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_seen: Mapped[float] = mapped_column(Float)


class OperatorTicketRow(Base):
    """Одноразовый CLI-ticket: в БД только hash, срок и факт использования."""

    __tablename__ = "operator_tickets"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CatalogAuditRow(Base):
    """Ограниченный технический аудит операторских действий без credential и документов."""

    __tablename__ = "catalog_operator_audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[float] = mapped_column(Float, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    event: Mapped[str] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class CatalogIdentityRow(Base):
    """Постоянная уникальная привязка настоящего GUID к внутреннему UUID, включая tombstones."""

    __tablename__ = "catalog_identities"
    onec_guid: Mapped[str] = mapped_column(String(36), primary_key=True)
    internal_guid: Mapped[str] = mapped_column(String(36), unique=True)
    code: Mapped[str] = mapped_column(String(128), index=True)
    tombstone_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CatalogEventRow(Base):
    """Durable inbox и dedup: технические GUID/action без карточек каталога или секретов."""

    __tablename__ = "catalog_events"
    __table_args__ = (
        UniqueConstraint("source", "event_id"),
        Index("ix_catalog_events_pending", "state", "received_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="onec_catalog")
    event_id: Mapped[str] = mapped_column(String(36))
    payload_hash: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[float] = mapped_column(Float)
    received_at: Mapped[float] = mapped_column(Float)
    changes: Mapped[list] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    applied_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    catalog_version: Mapped[str | None] = mapped_column(String(36), nullable=True)
