# alembic/versions/0003_catalog_sync_dashboard.py
"""Добавляет управление синхронизацией/агрегаты, сохраняя все прежние таблицы и snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт только новые таблицы; первый runtime bootstrap применит прежний env-флаг один раз."""
    op.create_table(
        "catalog_sync_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("initialized", sa.Boolean(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_run_at", sa.Float()),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("updated_by", sa.String(80), nullable=False),
        sa.Column("probe_result", sa.JSON(), nullable=False),
        sa.Column("metrics_started_at", sa.Float(), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "catalog_sync_settings",
            sa.column("id", sa.Integer()),
            sa.column("config", sa.JSON()),
            sa.column("revision", sa.Integer()),
            sa.column("initialized", sa.Boolean()),
            sa.column("updated_at", sa.Float()),
            sa.column("updated_by", sa.String()),
            sa.column("probe_result", sa.JSON()),
            sa.column("metrics_started_at", sa.Float()),
        ),
        [
            {
                "id": 1,
                "config": {"enabled": False, "time_utc": "15:00", "source_type": "local_xml"},
                "revision": 0,
                "initialized": False,
                "updated_at": 0,
                "updated_by": "migration",
                "probe_result": {},
                "metrics_started_at": 0,
            }
        ],
    )
    op.create_table(
        "catalog_sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_identity", sa.String(64), nullable=False),
        sa.Column("initiator", sa.String(32), nullable=False),
        sa.Column("schedule_key", sa.String(100), unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float()),
        sa.Column("finished_at", sa.Float()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
    )
    op.create_index("ix_catalog_sync_runs_job_id", "catalog_sync_runs", ["job_id"])
    op.create_index("ix_catalog_sync_runs_created_at", "catalog_sync_runs", ["created_at"])
    op.create_table(
        "dashboard_daily_metrics",
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("scope", sa.String(64), primary_key=True),
        *[
            sa.Column(key, sa.Integer(), nullable=False, server_default="0")
            for key in ("jobs", "positions", "matched", "manual", "review", "unresolved", "failed")
        ],
    )
    op.create_table(
        "service_activity",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("last_seen", sa.Float(), nullable=False),
    )
    op.create_table(
        "operator_tickets",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("consumed_at", sa.Float()),
    )
    op.create_index("ix_operator_tickets_expires_at", "operator_tickets", ["expires_at"])
    op.create_table(
        "catalog_operator_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_catalog_operator_audit_at", "catalog_operator_audit", ["at"])


def downgrade() -> None:
    """Удаляет только добавленные технические таблицы по отдельной явной команде владельца."""
    for name in (
        "catalog_operator_audit",
        "operator_tickets",
        "service_activity",
        "dashboard_daily_metrics",
        "catalog_sync_runs",
        "catalog_sync_settings",
    ):
        op.drop_table(name)
