# src/bootstrap.py
"""Composition root: единственное место сборки портов, адаптеров и прикладных операций."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.catalog import PruneCatalogUseCase, SynchronizeCatalogUseCase
from src.application.catalog_events import CatalogEvents
from src.application.catalog_management import CatalogManagement
from src.application.invoice_reports import invoice_export
from src.application.invoices.service import Invoices
from src.application.lifecycle import JobLifecycle
from src.application.mappings import MappingUseCases
from src.application.processing import JobProcessor
from src.application.specifications import Specifications
from src.config import Settings
from src.domain.embedding import compatible_embedding
from src.infrastructure.broker import RabbitBroker
from src.infrastructure.catalog_events import SqlCatalogEvents
from src.infrastructure.catalog_sources import CatalogSourceFactory
from src.infrastructure.http_clients import HttpExtractionSource, SharedEmbeddings
from src.infrastructure.integration_status import IntegrationProbes
from src.infrastructure.invoices.extracting import HttpInvoiceExtraction
from src.infrastructure.invoices.uploads import InvoiceUploadInspector
from src.infrastructure.operator_access import SqlOperatorAccess
from src.infrastructure.qdrant import QdrantIndex
from src.infrastructure.repositories import SqlCatalog, SqlJobs
from src.infrastructure.sync_repository import SqlSyncRepository
from src.infrastructure.xml_catalog import XmlCatalogSource


@dataclass
class Container:
    """Общий lifecycle клиентов и use-cases одного API/worker процесса."""

    settings: Settings
    engine: object
    http: httpx.AsyncClient
    jobs: SqlJobs
    catalog: SqlCatalog
    mappings: MappingUseCases
    processor: JobProcessor
    broker: RabbitBroker
    embeddings: SharedEmbeddings
    index: QdrantIndex
    pruner: PruneCatalogUseCase
    sync_repository: SqlSyncRepository
    catalog_sources: CatalogSourceFactory
    sync: CatalogManagement
    operator_access: SqlOperatorAccess
    integration_probes: IntegrationProbes
    catalog_events: CatalogEvents
    specifications: Specifications
    invoices: Invoices
    lifecycle: JobLifecycle

    async def ready(self) -> dict:
        """Проверяет реальные зависимости; недоступный shared endpoint никогда не
        считается ready.
        """

        async def probe(name: str, operation) -> tuple[str, bool]:
            """Изолирует сбои независимых зависимостей и не раскрывает их credentials."""
            try:
                async with asyncio.timeout(12):
                    await operation()
                return name, True
            except Exception:
                return name, False

        async def database() -> None:
            """Проверяет выполнение реального запроса и наличие применённой схемы."""
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM catalog_state LIMIT 1"))
                await connection.execute(text("SELECT 1 FROM catalog_events LIMIT 1"))
                await connection.execute(text("SELECT 1 FROM catalog_identities LIMIT 1"))

        async def embedding() -> None:
            """Сверяет реальную модель с fingerprint активного каталога."""
            identity = await self.embeddings.identity()
            catalog = await self.catalog.status()
            if catalog.get("ready") and not compatible_embedding(
                catalog.get("embedding_identity", {"fingerprint": catalog["fingerprint"]}), identity
            ):
                raise ValueError("Изменилась embedding identity")

        async def index() -> None:
            """Проверяет доступность Qdrant и число точек активного снимка."""
            status = await self.catalog.status()
            await self.index.request("GET", "/collections")
            if status["ready"] and await self.index.count(status["collection"]) != status["count"]:
                raise ValueError("Количество точек индекса отличается")

        dependencies = dict(
            await asyncio.gather(
                probe("database", database),
                probe("rabbitmq", self.broker.ready),
                probe("shared_embedding", embedding),
                probe("qdrant", index),
            )
        )
        try:
            catalog = await self.catalog.status()
            dependencies["catalog"] = catalog["ready"]
        except Exception:
            dependencies["catalog"] = False
        return {"ready": all(dependencies.values()), "dependencies": dependencies}

    async def close(self) -> None:
        """Освобождает pool и соединения одного процесса без изменений инфраструктуры."""
        await self.broker.close()
        await self.http.aclose()
        await self.engine.dispose()


def build_container(settings: Settings, database_url: str | None = None) -> Container:
    """Собирает production граф; подмена DSN предусмотрена исключительно для adapter tests."""
    engine = create_async_engine(database_url or settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    http = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=8),
        follow_redirects=False,
        trust_env=False,
    )
    jobs, catalog = SqlJobs(sessions, settings), SqlCatalog(sessions, engine)
    embeddings = SharedEmbeddings(http, settings)
    index = QdrantIndex(http, settings.qdrant_url, settings.qdrant_alias)
    source = XmlCatalogSource(
        settings.data_directory, settings.max_xml_bytes, settings.duplicate_code_policy
    )
    synchronizer = SynchronizeCatalogUseCase(
        source, catalog, embeddings, index, settings.embedding_batch_size
    )
    sources = CatalogSourceFactory(settings, http, source)
    sync_repository = SqlSyncRepository(sessions, settings, jobs)
    sync = CatalogManagement(sync_repository, sources, settings, lambda: jobs.clock())
    catalog_events = CatalogEvents(
        SqlCatalogEvents(sessions, settings, lambda: jobs.clock()),
        catalog,
        sources,
        sync,
        synchronizer,
        settings,
    )
    invoice_extraction = HttpInvoiceExtraction(http, settings)
    processor = JobProcessor(
        jobs,
        catalog,
        HttpExtractionSource(http, settings),
        embeddings,
        index,
        synchronizer,
        settings,
        sources,
        sync,
        catalog_events,
        invoice_extraction,
    )
    mappings = MappingUseCases(jobs, catalog, settings.max_items)
    return Container(
        settings,
        engine,
        http,
        jobs,
        catalog,
        mappings,
        processor,
        RabbitBroker(settings),
        embeddings,
        index,
        PruneCatalogUseCase(catalog, index),
        sync_repository,
        sources,
        sync,
        SqlOperatorAccess(sessions, settings, lambda: jobs.clock()),
        IntegrationProbes(http, settings),
        catalog_events,
        Specifications(jobs, mappings, settings.max_pdf_bytes),
        Invoices(
            jobs,
            MappingUseCases(
                jobs, catalog, settings.invoice_max_items, kind="invoice", exporter=invoice_export
            ),
            invoice_extraction,
            InvoiceUploadInspector(),
            settings,
        ),
        JobLifecycle(jobs),
    )
