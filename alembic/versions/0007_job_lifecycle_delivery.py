# alembic/versions/0007_job_lifecycle_delivery.py
"""Добавляет outbox, историю и ключи; прежние UUID/данные/leases и сообщения сохраняются."""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Неопределённые исторические публикации требуют drain и ограниченного reconciliation."""
    for name, kind, default in [
        ("delivery_state", sa.String(24), "pending"),
        ("delivery_generation", sa.Integer(), "1"),
        ("delivery_next_at", sa.Float(), "0"),
        ("delivery_attempts", sa.Integer(), "0"),
        ("delivery_recoveries", sa.Integer(), "0"),
        ("delivery_noops", sa.Integer(), "0"),
        ("last_event_id", sa.Integer(), "0"),
        ("temporary_bytes", sa.BigInteger(), "0"),
    ]:
        op.add_column("mapping_jobs", sa.Column(name, kind, nullable=False, server_default=default))
    op.add_column(
        "mapping_jobs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "mapping_jobs",
        sa.Column("lifecycle_metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    for name, kind in [
        ("delivery_token", sa.String(36)),
        ("delivery_until", sa.Float()),
        ("delivery_error", sa.String(64)),
        ("delivery_published_at", sa.Float()),
        ("delivery_received_at", sa.Float()),
        ("started_at", sa.Float()),
        ("updated_at", sa.Float()),
        ("active_fingerprint", sa.String(64)),
    ]:
        op.add_column("mapping_jobs", sa.Column(name, kind, nullable=True))
    op.execute("""UPDATE mapping_jobs SET updated_at=created_at, delivery_state=CASE
        WHEN state IN ('complete','ready_for_review','approved','failed','cancelled') THEN 'closed'
        WHEN lease_token IS NOT NULL THEN 'received'
        WHEN dispatched_at IS NOT NULL THEN 'legacy_unknown' ELSE 'pending' END""")
    op.execute("UPDATE mapping_jobs SET delivery_published_at=dispatched_at")
    op.execute("""UPDATE mapping_jobs SET temporary_bytes=409600+length(CAST(data AS TEXT))
        +COALESCE((SELECT sum(length(content)) FROM invoice_uploads u
                   WHERE u.job_id=mapping_jobs.id),0)
        +COALESCE((SELECT length(content) FROM specification_uploads u
                   WHERE u.job_id=mapping_jobs.id),0)
        +COALESCE((SELECT length(CAST(snapshot AS TEXT)) FROM mapping_approvals a
                   WHERE a.job_id=mapping_jobs.id),0)""")
    op.create_index("ix_mapping_jobs_delivery_state", "mapping_jobs", ["delivery_state"])
    op.create_index(
        "uq_active_job_input", "mapping_jobs", ["owner", "active_fingerprint"], unique=True
    )
    op.create_table(
        "mapping_job_keys",
        sa.Column("owner", sa.String(80), primary_key=True),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("mapping_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_table(
        "mapping_job_events",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("mapping_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """Предусмотрен только явный откат владельцем; штатный rollback оставляет additive schema."""
    op.drop_table("mapping_job_events")
    op.drop_table("mapping_job_keys")
    op.drop_index("uq_active_job_input", table_name="mapping_jobs")
    op.drop_index("ix_mapping_jobs_delivery_state", table_name="mapping_jobs")
    for name in [
        "delivery_state",
        "delivery_generation",
        "delivery_next_at",
        "delivery_attempts",
        "delivery_recoveries",
        "delivery_noops",
        "last_event_id",
        "temporary_bytes",
        "cancel_requested",
        "delivery_token",
        "delivery_until",
        "delivery_error",
        "delivery_published_at",
        "delivery_received_at",
        "started_at",
        "updated_at",
        "active_fingerprint",
        "lifecycle_metadata",
    ]:
        op.drop_column("mapping_jobs", name)
