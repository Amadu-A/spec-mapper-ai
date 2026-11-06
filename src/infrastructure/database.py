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
