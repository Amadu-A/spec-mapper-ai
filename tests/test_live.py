# tests/test_live.py
"""Только реальные PostgreSQL/Qdrant/shared-embedding/RabbitMQ; запуск после scripts/up.sh."""

import asyncio
from dataclasses import replace
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text

from src.application.catalog import SynchronizeCatalogUseCase
from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import CatalogItem, ParsedCatalog, digest
from src.infrastructure.broker import RabbitBroker
from src.infrastructure.database import Base, CatalogVersionRow, JobRow
from src.infrastructure.http_clients import HttpExtractionSource
from src.infrastructure.qdrant import QdrantIndex

pytestmark = pytest.mark.live


async def test_live_incremental_catalog_keeps_unchanged_vectors():
    """Реальные PostgreSQL/Qdrant/embedding проверяют delta в отдельной тестовой SQL-схеме."""
    settings = Settings()
    namespace = "spec_mapper_smoke_" + uuid4().hex
    control = build_container(settings)
    isolated = build_container(
        settings.model_copy(update={"qdrant_alias": namespace}),
        settings.database_url + "?options=" + quote("-csearch_path=" + namespace, safe=""),
    )
    created = False

    class SnapshotSource:
        """Синтетический справочник теста; внешние SQL/HTTP адаптеры остаются настоящими."""

        items = [
            CatalogItem(str(uuid4()), "TEST-001", "Клапан DN50 PN16", "шт"),
            CatalogItem(str(uuid4()), "TEST-002", "Клапан DN40 PN16", "шт"),
            CatalogItem(str(uuid4()), "TEST-003", "Насос 40 Вт", "шт"),
        ]

        async def read(self):
            """Возвращает проверенный снимок фиксированных тестовых наименований."""
            return ParsedCatalog(self.items, digest([item.to_dict() for item in self.items]))

    class CountedEmbeddings:
        """Считает векторизацию позиций, делегируя реальные запросы shared-embedding."""

        count = 0

        async def identity(self):
            """Проверяет настоящую модель отдельно от счётчика позиций справочника."""
            return await isolated.embeddings.identity()

        async def embed(self, texts):
            """Делегирует векторизацию настоящему сервису без fake векторов."""
            self.count += len(texts)
            return await isolated.embeddings.embed(texts)

    async def progress(stage, **details):
        """Не сохраняет тестовые тексты или служебные реквизиты в журнал."""
        return None

    try:
        async with control.engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA " + namespace))
        created = True
        async with isolated.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        source, embeddings = SnapshotSource(), CountedEmbeddings()
        sync = SynchronizeCatalogUseCase(source, isolated.catalog, embeddings, isolated.index, 3)
        first = await sync.execute(progress)
        assert embeddings.count == 3
        source.items = [
            source.items[0],
            replace(source.items[1], name="Клапан DN65 PN16"),
            CatalogItem(str(uuid4()), "TEST-004", "Насос 60 Вт", "шт"),
        ]
        second = await sync.execute(progress)
        assert second["mode"] == "incremental"
        assert second["embedded_count"] == second["indexed_count"] == 2
        assert second["removed_count"] == second["reused_count"] == 1
        assert embeddings.count == 5
        assert await isolated.index.count(first["collection"]) == 3
        assert await isolated.index.count(second["collection"]) == 3
        repeated = await sync.execute(progress)
        assert repeated["version"] == second["version"] and embeddings.count == 5
    finally:
        if created:
            status = await isolated.catalog.status()
            if status["ready"]:
                await isolated.index.restore(None)
            async with isolated.jobs.sessions() as session:
                versions = list(await session.scalars(select(CatalogVersionRow)))
            for version in versions:
                collection = version.metadata_json["collection"]
                if await isolated.index.exists(collection):
                    await isolated.index.remove(collection)
        await isolated.close()
        if created:
            async with control.engine.begin() as connection:
                await connection.execute(text("DROP SCHEMA " + namespace + " CASCADE"))
        await control.close()


async def test_live_onec_http_chain_and_owner_isolation():
    """Реальный API/worker: JSON, Top-5, поиск, выбор/null, revision, approval и чужой ключ."""
    settings = Settings()
    if not {"onec", "telegram"} <= settings.api_keys.keys():
        pytest.skip("Для цепочки нужны API_KEYS с отдельными onec и telegram")
    headers = {
        "X-API-Key": settings.api_keys["onec"].get_secret_value(),
        "Idempotency-Key": "live-onec-" + uuid4().hex,
        "X-Source-Job-ID": str(uuid4()),
    }
    other = {"X-API-Key": settings.api_keys["telegram"].get_secret_value()}
    row = {
        "Caption": "Тестовое сопоставление DN50 PN16",
        "Units": "шт",
        "Quantity": " 2,50 ",
        "UserComment1": "TEST-MAPPER",
        "Pages": "1–2",
    }
    source = {"items": [row.copy(), row.copy()]}
    container = build_container(settings)
    job_id = None
    try:
        async with httpx.AsyncClient(
            base_url=settings.app_public_base_url, headers=headers, timeout=30, trust_env=False
        ) as client:
            ready = await client.get("/health/ready")
            assert ready.status_code == 200, "Сначала завершите scripts/up.sh"
            response = await client.post("/api/v1/mappings/import-json", json=source)
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            path = "/api/v1/mappings/" + job_id
            repeat = await client.post("/api/v1/mappings/import-json", json=source)
            assert repeat.json()["job_id"] == job_id
            for _ in range(60):
                status = (await client.get(path)).json()
                if status["status"] in {"complete", "ready_for_review", "failed"}:
                    break
                await asyncio.sleep(1)
            assert status["status"] in {"complete", "ready_for_review"}
            candidates = (await client.get(path + "/candidates?row_index=0")).json()
            catalog = (await client.get("/api/v1/catalog/status")).json()
            assert len(candidates["candidates"]) == min(5, catalog["count"])
            found = (await client.get("/api/v1/catalog/items?q=0&limit=1")).json()["items"]
            assert found, "В тестовом/реальном каталоге ожидается хотя бы один код с 0"
            selected = await client.put(
                path + "/rows/0/selection",
                json={"catalog_item_guid": found[0]["guid"], "revision": status["revision"]},
            )
            assert selected.status_code == 200
            stale = await client.put(
                path + "/rows/1/selection",
                json={"catalog_item_guid": None, "revision": status["revision"]},
            )
            assert stale.status_code == 409
            selected = await client.put(
                path + "/rows/1/selection",
                json={"catalog_item_guid": None, "revision": selected.json()["revision"]},
            )
            assert selected.status_code == 200
            revision = selected.json()["revision"]
            approved = await client.post(path + "/approve", json={"revision": revision})
            assert approved.status_code == 200
            assert approved.json()["expires_at"] == status["expires_at"]
            snapshot = (await client.get(path + "/approved.json")).json()
            assert snapshot["status"] == "approved" and snapshot["approval"]["owner"] == "api:onec"
            assert snapshot["items"][0]["onec"]["code"] == found[0]["code"]
            assert snapshot["items"][1]["onec"] is None
            assert (await client.get(path + "/source.json")).json() == source
            assert (await client.get(path + "/result.xlsx")).content.startswith(b"PK")
            assert (
                await client.post(path + "/approve", json={"revision": revision})
            ).status_code == 200
            assert (await client.get(path + "/approved.json")).json() == snapshot
            assert (
                await client.put(
                    path + "/rows/0/selection",
                    json={"catalog_item_guid": None, "revision": revision},
                )
            ).status_code == 409
            for suffix in ("", "/result", "/approved.json", "/source.json", "/result.xlsx"):
                response = await client.get(path + suffix, headers=other)
                assert response.status_code == 404
    finally:
        if job_id:
            async with container.jobs.sessions.begin() as session:
                await session.execute(delete(JobRow).where(JobRow.id == job_id))
        await container.close()


async def test_live_upstream_contract(request):
    """По явно указанному UUID проверяет настоящее извлечение и строгую исходную пятёрку."""
    job_id = request.config.getoption("--source-job-id")
    if job_id is None:
        pytest.skip("Передайте --source-job-id UUID готового upstream API-задания")
    container = build_container(Settings())
    try:

        async def progress(stage, **details):
            """Принимает этапы readonly проверки без сохранения документа в артефакты."""
            return None

        payload = await HttpExtractionSource(container.http, container.settings).fetch(
            job_id, progress
        )
        assert isinstance(payload["items"], list)
    finally:
        await container.close()


async def test_live_postgres_migration_outbox_and_fencing():
    """Проверяет настоящую PostgreSQL схему и lease CAS; удаляет только собственный smoke job."""
    settings = Settings()
    settings.require_secrets()
    container = build_container(settings)
    job_id = None
    try:
        async with container.engine.connect() as connection:
            assert "PostgreSQL" in await connection.scalar(text("SELECT version()"))
        job = await container.jobs.create("smoke:" + str(uuid4()), "live", {}, "catalog")
        job_id = job["id"]
        assert job_id in await container.jobs.pending()
        first = await container.jobs.claim(job_id)
        assert first and await container.jobs.claim(job_id) is None
        assert not await container.jobs.checkpoint(job_id, str(uuid4()), stage="bad")
        assert await container.jobs.checkpoint(job_id, first["lease_token"], state="complete")
    finally:
        if job_id:
            async with container.jobs.sessions.begin() as session:
                await session.execute(delete(JobRow).where(JobRow.id == job_id))
        await container.close()


async def test_live_embedding_qdrant_batch_and_alias():
    """Использует real shared vectors и изолированную smoke коллекцию, сохраняя рабочий каталог."""
    container = build_container(Settings())
    name = "spec_mapper_smoke_" + uuid4().hex
    index = QdrantIndex(container.http, container.settings.qdrant_url, name + "_alias")
    created = switched = False
    try:
        identity = await container.embeddings.identity()
        vectors = await container.embeddings.embed(["Клапан DN50 PN16", "Насос циркуляционный"])
        await index.create(name, identity["dimension"])
        created = True
        ids = [str(uuid4()), str(uuid4())]
        await index.put(
            name,
            [{"id": guid, "vector": vector} for guid, vector in zip(ids, vectors, strict=True)],
        )
        assert await index.count(name) == 2
        assert len(await index.search(name, vectors, 2)) == 2
        await index.clone(name, name + "_copy")
        assert await index.count(name + "_copy") == 2
        await index.delete_points(name + "_copy", [ids[1]])
        assert await index.count(name + "_copy") == 1
        assert await index.count(name) == 2
        await index.switch(name + "_copy")
        switched = True
        found = await index.search(name + "_alias", [vectors[0]], 1)
        assert found[0][0][0] == ids[0]
    finally:
        if switched:
            await index.restore(None)
        if created:
            await index.remove(name + "_copy")
            await index.request("DELETE", "/collections/" + name)
        await container.close()


async def test_live_rabbit_publisher_confirm_and_redelivery():
    """Проверяет durable publish, nack/redelivery и ack на собственной временной smoke очереди."""
    settings = Settings().model_copy(update={"rabbitmq_queue": "spec_mapper.smoke." + uuid4().hex})
    broker = RabbitBroker(settings)
    try:
        job_id = str(uuid4())
        await broker.publish(job_id)
        first = await broker.queue.get(timeout=10)
        assert first.body.decode() == job_id
        await first.nack(requeue=True)
        second = await broker.queue.get(timeout=10)
        assert second.redelivered
        assert second.body == first.body
        await second.ack()
        await broker.queue.delete(if_unused=False, if_empty=False)
    finally:
        await broker.close()


async def test_live_qdrant_snapshot_delta_and_alias_without_model():
    """Проверяет настоящий Qdrant на тестовых векторах, независимо от доступности модели."""
    container = build_container(Settings())
    name = "spec_mapper_smoke_" + uuid4().hex
    index = QdrantIndex(container.http, container.settings.qdrant_url, name + "_alias")
    created = switched = False
    try:
        ids = [str(uuid4()), str(uuid4())]
        await index.create(name, 2)
        created = True
        await index.put(
            name, [{"id": ids[0], "vector": [1.0, 0.0]}, {"id": ids[1], "vector": [0.0, 1.0]}]
        )
        await index.clone(name, name + "_copy")
        assert await index.count(name + "_copy") == 2
        await index.delete_points(name + "_copy", [ids[1]])
        assert await index.count(name + "_copy") == 1
        assert await index.count(name) == 2
        await index.switch(name + "_copy")
        switched = True
        assert (await index.search(name + "_alias", [[1.0, 0.0]], 1))[0][0][0] == ids[0]
        await index.switch(name)
        assert await index.count(name + "_alias") == 2
    finally:
        if switched:
            await index.restore(None)
        if created:
            await index.remove(name + "_copy")
            await index.remove(name)
        await container.close()
