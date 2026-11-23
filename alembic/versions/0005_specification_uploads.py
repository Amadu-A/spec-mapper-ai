# alembic/versions/0005_specification_uploads.py
"""Добавляет временное хранилище PDF; прежние задания, каталог и approvals сохраняются."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Связывает PDF с заданием для атомарного принятия и удаления после TTL."""
    op.create_table(
        "specification_uploads",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("mapping_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    """Удаляет только добавленное хранилище PDF при явно выбранном откате миграции."""
    op.drop_table("specification_uploads")
