# tests/test_dashboard_migrations.py
"""Upgrade 0002→0003 с прежними данными и конкуренция двух PostgreSQL scheduler replicas."""

import asyncio
import importlib.util
from pathlib import Path
from time import time
from urllib.parse import quote
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import Settings
from src.domain.models import digest
from src.infrastructure.database import (
    ApprovalRow,
    CatalogStateRow,
    CatalogVersionRow,
    JobRow,
    SyncRunRow,
)
from src.infrastructure.repositories import SqlJobs
from src.infrastructure.sync_repository import SqlSyncRepository


def upgrade(connection, revision):
    """Исполняет настоящую Alembic revision на изолированном SQL соединении."""
    path = next(Path("alembic/versions").glob(revision + "_*.py"))
    spec = importlib.util.spec_from_file_location("migration_" + revision, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()


async def check_upgrade(engine):
    """Сохраняет активную версию, job и immutable approval при добавлении новых таблиц."""
    now = time()
    job_id = str(uuid4())
    version = str(uuid4())
    snapshot = {"approved": "literal untouched"}
    async with engine.begin() as connection:
        for revision in ["0001", "0002"]:
            await connection.run_sync(upgrade, revision)
        await connection.execute(
            insert(CatalogVersionRow).values(id=version, metadata_json={"count": 1}, created_at=now)
        )
        await connection.execute(insert(CatalogStateRow).values(id=1, active=version, syncing=True))
        await connection.execute(
            insert(JobRow).values(
                id=job_id,
                owner="legacy-owner",
                idempotency_hash="a" * 64,
                request_hash="b" * 64,
                kind="mapping",
                state="approved",
                stage="approved",
                created_at=now,
                expires_at=now + 3600,
                attempts=1,
                revision=3,
                data={"approval": snapshot},
            )
        )
        await connection.execute(
            insert(ApprovalRow).values(
                job_id=job_id,
                owner="legacy-owner",
                revision=3,
                approved_at=now,
                idempotency_hash="c" * 64,
                snapshot=snapshot,
            )
        )
        await connection.run_sync(upgrade, "0003")
        assert await connection.scalar(select(CatalogStateRow.active)) == version
        assert await connection.scalar(select(CatalogStateRow.syncing)) is True
        assert await connection.scalar(select(ApprovalRow.snapshot)) == snapshot
        assert await connection.scalar(select(JobRow.data)) == {"approval": snapshot}


async def test_additive_migration_preserves_data_sqlite(tmp_path):
    """Offline smoke исполняет все три SQL revisions, не заменяя их metadata.create_all."""
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "migration.db").as_posix())
    try:
        await check_upgrade(engine)
    finally:
        await engine.dispose()


@pytest.mark.live
async def test_live_postgresql_upgrade_and_two_scheduler_replicas():
    """Настоящий PostgreSQL проверяется в уникальной SQL-схеме без затрагивания public/volumes."""
    settings = Settings()
    name = "mapper_v2_migration_" + uuid4().hex
    control = create_async_engine(settings.database_url)
    engine = create_async_engine(
        settings.database_url + "?options=" + quote("-csearch_path=" + name, safe="")
    )
    created = False
    try:
        async with control.begin() as connection:
            await connection.execute(text("CREATE SCHEMA " + name))
            created = True
        await check_upgrade(engine)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        clock = [1793977140.0]
        jobs = SqlJobs(sessions, settings, clock=lambda: clock[0])
        first, second = (
            SqlSyncRepository(sessions, settings, jobs),
            SqlSyncRepository(sessions, settings, jobs),
        )
        config = await first.settings()
        config.update(enabled=True, time_utc="15:00")
        await first.save(config, config["revision"], "operator")
        clock[0] += 60
        identity = digest("live scheduler source")
        results = await asyncio.gather(first.schedule(identity), second.schedule(identity))
        assert len([value for value in results if value]) == 1
        async with sessions() as session:
            runs = list(await session.scalars(select(SyncRunRow)))
            assert len(runs) == 1
        assert (await second.settings())["next_run_at"] == 1794063600.0
    finally:
        await engine.dispose()
        if created:
            async with control.begin() as connection:
                await connection.execute(text("DROP SCHEMA " + name + " CASCADE"))
        await control.dispose()
