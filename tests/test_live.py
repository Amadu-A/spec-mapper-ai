# tests/test_live.py
"""Только реальные PostgreSQL/Qdrant/shared-embedding/RabbitMQ; запуск после scripts/up.sh."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from src.bootstrap import build_container
from src.config import Settings
from src.infrastructure.broker import RabbitBroker
from src.infrastructure.database import JobRow
from src.infrastructure.http_clients import HttpExtractionSource
from src.infrastructure.qdrant import QdrantIndex

pytestmark = pytest.mark.live


async def test_live_upstream_contract(request):
    """По явно указанному UUID проверяет настоящее извлечение и строгую исходную пятёрку."""
    job_id = request.config.getoption("--source-job-id")
    if job_id is None:
        pytest.skip("Передайте --source-job-id UUID готового upstream API-задания")
    container = build_container(Settings())
    try:

        async def progress(stage):
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
