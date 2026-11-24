# tests/live_invoice_runtime.py
"""Только live QA: общий API/worker с изолированной SQL-схемой и read-only реальным каталогом.

Не является production entrypoint. Требует уникальные QA_SCHEMA и проектную очередь;
не создаёт модели, каталоги, коллекции, пользователей RabbitMQ или Docker networks.
Управление временными контейнерами выполняет тестовый запуск вне этого модуля.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

from pydantic import Field
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.bootstrap import build_container
from src.config import Settings
from src.infrastructure.database import ApprovalRow, JobRow, SpecificationUploadRow
from src.infrastructure.repositories import SqlCatalog
from src.main import create_app


class QaSettings(Settings):
    """Pydantic-конфигурация изолированной проверки; не добавляет QA-параметры в production."""

    qa_schema: str = Field(pattern=r"^mapper_invoice_qa_[a-f0-9]{16}$")


def checked_settings() -> QaSettings:
    """Не позволяет тестовому entrypoint использовать рабочую очередь или общую SQL-схему."""
    settings = QaSettings()
    suffix = settings.qa_schema.removeprefix("mapper_invoice_qa_")
    if settings.rabbitmq_queue != "spec_mapper.invoice_qa." + suffix:
        raise ValueError("Нужна отдельная очередь live QA")
    settings.catalog_daily_sync = False
    settings.catalog_staging_directory = Path("/tmp/invoice-qa-staging")
    return settings


def qa_database_url(settings: QaSettings) -> str:
    """Ограничивает все транзакции тестовых jobs отдельной схемой PostgreSQL."""
    return (
        settings.database_url + "?options=" + quote("-csearch_path=" + settings.qa_schema, safe="")
    )


def live_container(settings: QaSettings | None = None):
    """Собирает тот же production graph; только catalog port читает public через read-only pool."""
    settings = checked_settings() if settings is None else settings
    container = build_container(settings, qa_database_url(settings))
    readonly = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    catalog = SqlCatalog(async_sessionmaker(readonly, expire_on_commit=False), readonly)
    container.catalog = catalog
    container.mappings.catalog = catalog
    container.invoices.mappings.catalog = catalog
    container.processor.catalog = catalog
    original_close = container.close

    async def close() -> None:
        """Закрывает оба собственных pool, не меняя реальные каталог/индекс/сервисы."""
        await original_close()
        await readonly.dispose()

    container.close = close
    return container


def api():
    """Создаёт обычный FastAPI с реальными SQL/HTTP/AMQP/embedding/Qdrant адаптерами."""
    return create_app(live_container())


async def migrate() -> None:
    """Проверяет PostgreSQL 0005→0006 на синтетическом старом job/PDF/approval в своей схеме."""
    from test_dashboard_migrations import upgrade

    settings = checked_settings()
    control = create_async_engine(settings.database_url)
    engine = create_async_engine(qa_database_url(settings))
    identifier = "00000000-0000-4000-8000-000000000001"
    snapshot = {"items": [{"Caption": "Тестовый исторический снимок"}]}
    try:
        async with control.begin() as connection:
            await connection.execute(text("CREATE SCHEMA " + settings.qa_schema))
        async with engine.begin() as connection:
            for revision in ["0001", "0002", "0003", "0004", "0005"]:
                await connection.run_sync(upgrade, revision)
            await connection.execute(
                insert(JobRow).values(
                    id=identifier,
                    owner="qa-legacy",
                    kind="mapping",
                    idempotency_hash="a" * 64,
                    request_hash="b" * 64,
                    state="approved",
                    stage="approved",
                    created_at=1,
                    expires_at=2,
                    attempts=1,
                    revision=1,
                    data={"source": snapshot},
                )
            )
            await connection.execute(
                insert(SpecificationUploadRow).values(job_id=identifier, content=b"%PDF-preserved")
            )
            await connection.execute(
                insert(ApprovalRow).values(
                    job_id=identifier,
                    owner="qa-legacy",
                    revision=1,
                    approved_at=1,
                    idempotency_hash="c" * 64,
                    snapshot=snapshot,
                )
            )
            await connection.run_sync(upgrade, "0006")
            assert await connection.scalar(select(ApprovalRow.snapshot)) == snapshot
            assert (
                await connection.scalar(select(SpecificationUploadRow.content)) == b"%PDF-preserved"
            )
            assert "invoice_uploads" in await connection.run_sync(
                lambda conn: inspect(conn).get_table_names()
            )
        print(
            json.dumps({"postgresql_migration": "0005_to_0006", "legacy_data_preserved": True}),
            flush=True,
        )
    finally:
        await engine.dispose()
        await control.dispose()


async def cleanup() -> None:
    """Удаляет только уникальные QA schema/queue после остановки своих API/worker контейнеров."""
    settings = checked_settings()
    from src.infrastructure.broker import RabbitBroker

    broker = RabbitBroker(settings)
    control = create_async_engine(settings.database_url)
    try:
        await broker.connect()
        await broker.queue.delete(if_unused=False, if_empty=False)
        async with control.begin() as connection:
            await connection.execute(
                text("DROP SCHEMA IF EXISTS " + settings.qa_schema + " CASCADE")
            )
        print(json.dumps({"qa_resources_removed": True}), flush=True)
    finally:
        await broker.close()
        await control.dispose()


def main() -> None:
    """Запускает выбранную часть проверки; секреты и текст ошибок драйверов не печатаются."""
    try:
        action = sys.argv[1]
        if action == "api":
            import uvicorn

            uvicorn.run(api(), host="127.0.0.1", port=8087, access_log=False, proxy_headers=False)
        elif action == "worker":
            import src.worker as worker

            worker.Settings = checked_settings
            worker.build_container = live_container
            asyncio.run(worker.run())
        elif action == "migrate":
            asyncio.run(migrate())
        elif action == "cleanup":
            asyncio.run(cleanup())
        else:
            raise ValueError("Неизвестный QA action")
    except Exception as error:
        print(json.dumps({"qa_error": type(error).__name__}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
