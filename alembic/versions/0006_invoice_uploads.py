# alembic/versions/0006_invoice_uploads.py
"""Добавляет временные blobs счетов, не изменяя спецификации, каталог и approvals."""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт хранилище пакета, атомарно связанное с общим долговечным заданием."""
    op.create_table(
        "invoice_uploads",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("mapping_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("file_index", sa.Integer(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
    )


def downgrade() -> None:
    """Откатывает только новую таблицу по явной команде владельца."""
    op.drop_table("invoice_uploads")
