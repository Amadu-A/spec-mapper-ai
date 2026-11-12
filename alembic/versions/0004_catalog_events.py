# alembic/versions/0004_catalog_events.py
"""Добавляет durable события и GUID registry без изменения каталога, jobs и approvals."""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт только новые таблицы и индексы; старые snapshots не переписывает."""
    op.create_table(
        "catalog_identities",
        sa.Column("onec_guid", sa.String(36), primary_key=True),
        sa.Column("internal_guid", sa.String(36), nullable=False, unique=True),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("tombstone_at", sa.Float()),
    )
    op.create_index("ix_catalog_identities_code", "catalog_identities", ["code"])
    op.create_table(
        "catalog_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("job_id", sa.String(36)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(80)),
        sa.Column("applied_at", sa.Float()),
        sa.Column("catalog_version", sa.String(36)),
        sa.UniqueConstraint("source", "event_id"),
    )
    op.create_index("ix_catalog_events_pending", "catalog_events", ["state", "received_at"])
    op.create_index("ix_catalog_events_job_id", "catalog_events", ["job_id"])


def downgrade() -> None:
    """Требует отдельного решения владельца: удаляет только новые таблицы V4."""
    op.drop_table("catalog_events")
    op.drop_table("catalog_identities")
