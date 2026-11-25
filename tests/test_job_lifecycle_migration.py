# tests/test_job_lifecycle_migration.py
"""Настоящие SQL revisions: классификация старых сообщений без timer replay и потерь данных."""

from time import time
from uuid import uuid4

from sqlalchemy import MetaData, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from test_dashboard_migrations import upgrade

from src.infrastructure.database import ApprovalRow, JobRow, SpecificationUploadRow


async def test_0007_preserves_checkpoints_blobs_approval_and_unknown_delivery(tmp_path):
    """Pending, неопределённая публикация, старый lease и terminal получают разные состояния."""
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "old.db").as_posix())
    now = time()
    fixtures = [
        ("queued", None, None, "pending"),
        ("queued", now - 60, None, "legacy_unknown"),
        ("matching", now - 60, str(uuid4()), "received"),
        ("approved", now - 60, None, "closed"),
    ]
    identifiers, source = [], {"source": {"items": [{"Caption": "000123 = 2"}]}}
    try:
        async with engine.begin() as connection:
            for revision in ["0001", "0002", "0003", "0004", "0005", "0006"]:
                await connection.run_sync(upgrade, revision)
            table = await connection.run_sync(
                lambda conn: Table("mapping_jobs", MetaData(), autoload_with=conn)
            )
            for index, (state, dispatched, token, _) in enumerate(fixtures):
                identifier = str(uuid4())
                identifiers.append(identifier)
                await connection.execute(
                    insert(table).values(
                        id=identifier,
                        owner="migration",
                        kind="mapping",
                        idempotency_hash=str(index) * 64,
                        request_hash="f" * 64,
                        state=state,
                        stage=state,
                        created_at=now,
                        expires_at=now + 3600,
                        attempts=1,
                        revision=7,
                        dispatched_at=dispatched,
                        lease_token=token,
                        lease_until=now + 120 if token else None,
                        data=source,
                    )
                )
            await connection.execute(
                insert(SpecificationUploadRow).values(
                    job_id=identifiers[-1],
                    content=b"%PDF-preserved",
                )
            )
            await connection.execute(
                insert(ApprovalRow).values(
                    job_id=identifiers[-1],
                    owner="migration",
                    revision=7,
                    approved_at=now,
                    idempotency_hash="e" * 64,
                    snapshot=source,
                )
            )
            await connection.run_sync(upgrade, "0007")
            for identifier, (_, dispatched, token, expected) in zip(
                identifiers, fixtures, strict=True
            ):
                row = (
                    (
                        await connection.execute(
                            select(JobRow.__table__).where(JobRow.id == identifier)
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["delivery_state"] == expected
                assert row["data"] == source and row["revision"] == 7
                assert row["lease_token"] == token and row["dispatched_at"] == dispatched
                assert row["delivery_generation"] == 1 and row["temporary_bytes"] >= 409600
            assert await connection.scalar(select(ApprovalRow.snapshot)) == source
            assert (
                await connection.scalar(select(SpecificationUploadRow.content)) == b"%PDF-preserved"
            )
    finally:
        await engine.dispose()
