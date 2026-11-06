# alembic/versions/0001_initial.py
"""Начальная схема отдельного spec-mapper; старая учебная БД не мигрируется и не удаляется."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт durable jobs, versioned каталог, активный указатель и постоянный vector cache."""
    op.create_table(
        "mapping_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float()),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("dispatched_at", sa.Float()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner", "idempotency_hash"),
    )
    for name in ("owner", "state", "expires_at"):
        op.create_index("ix_mapping_jobs_" + name, "mapping_jobs", [name])
    op.create_table(
        "catalog_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "catalog_entries",
        sa.Column("version", sa.String(36), primary_key=True),
        sa.Column("guid", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(32000), nullable=False),
        sa.Column("article", sa.String(32000), nullable=False),
        sa.Column("item_data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_catalog_search", "catalog_entries", ["version", "code"])
    op.create_table(
        "catalog_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active", sa.String(36)),
        sa.Column("previous", sa.String(36)),
        sa.Column("last_successful_sync", sa.Float()),
        sa.Column("last_error", sa.String(80)),
        sa.Column("syncing", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "catalog_vectors",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("vector", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """Удаляет только таблицы этой ревизии по явной операторской команде Alembic downgrade."""
    for name in (
        "catalog_vectors",
        "catalog_state",
        "catalog_entries",
        "catalog_versions",
        "mapping_jobs",
    ):
        op.drop_table(name)
