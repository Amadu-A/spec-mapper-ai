# alembic/versions/0002_approvals.py
"""Добавляет неизменяемые утверждённые результаты без изменения существующих заданий."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт approval snapshot с каскадной очисткой только после TTL исходного задания."""
    op.create_table(
        "mapping_approvals",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("mapping_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.Float(), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """Откатывает только новую таблицу по отдельной операторской команде."""
    op.drop_table("mapping_approvals")
