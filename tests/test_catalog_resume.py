# tests/test_catalog_resume.py
"""Восстановление длительного импорта и независимость его deadline от временных документов."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.domain.models import MapperError, digest


async def test_startup_retries_failed_import_and_repairs_missing_index(system, monkeypatch):
    """Повтор up не застревает на failed job и заново проверяет реальное наличие индекса."""
    from src import cli

    container, _, _, catalog_source, _ = system
    published = []

    async def publish(job_id):
        """Доставляет настоящее durable задание синхронно вместо RabbitMQ."""
        published.append(job_id)
        await container.processor.process(job_id)

    async def close():
        """Закрывает только тестовый broker без внешнего соединения."""
        return None

    container.broker = SimpleNamespace(publish=publish, close=close)
    monkeypatch.setattr(cli, "build_container", lambda _: container)
    monkeypatch.setattr(cli, "XmlCatalogSource", lambda *args: catalog_source)
    status = await container.catalog.status()
    key = "startup:" + digest(["xml1", "fp1", status["last_successful_sync"]])
    failed = await container.jobs.create("system", key, {}, "catalog")
    claimed = await container.jobs.claim(failed["id"])
    await container.jobs.checkpoint(
        failed["id"], claimed["lease_token"], state="failed", error_code="embedding_unavailable"
    )
    await cli.enqueue_catalog(wait=True)
    assert published[-1] != failed["id"]
    assert (await container.jobs.get(published[-1]))["state"] == "complete"
    container.index.collections.pop(status["collection"])
    calls = container.embeddings.calls
    await cli.enqueue_catalog(wait=True)
    repaired = await container.catalog.status()
    assert repaired["version"] != status["version"]
    assert repaired["count"] == await container.index.count(repaired["collection"]) == 1
    assert container.embeddings.calls == calls


async def progress(stage, **details):
    """Принимает безопасные counts и версии standalone синхронизации в offline tests."""
    return None


async def test_catalog_has_own_deadline_and_staging_resumes(system, source, monkeypatch):
    """Возобновляет незавершённую пачку без нового staging и повторной векторизации."""
    container, item, sync, catalog_source, _ = system
    catalog = await container.jobs.create("admin", "long", {}, "catalog")
    mapping = await container.mappings.import_document("owner", "short", source)
    assert catalog["expires_at"] - catalog["created_at"] >= 86400
    assert mapping["expires_at"] - mapping["created_at"] <= 3600.01
    catalog_source.source_hash = "resume"
    catalog_source.items = [item] + [
        replace(item, guid=str(uuid4()), code=str(i), name=f"Позиция {i}") for i in range(4)
    ]
    original = container.index.put
    attempts = 0

    async def fail_second(collection, points):
        """Эмулирует отказ Qdrant после сохранённого offset первой пачки."""
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise MapperError("qdrant_unavailable", "Тестовый сбой", 503)
        await original(collection, points)

    monkeypatch.setattr(container.index, "put", fail_second)
    with pytest.raises(MapperError):
        await sync.execute(progress)
    saved = await container.catalog.resumable("resume", {"fingerprint": "fp1"})
    assert saved["resume_offset"] == 2
    stage_collection = saved["collection"]
    count_before = len(container.index.collections)
    calls_before = container.embeddings.calls
    monkeypatch.setattr(container.index, "put", original)
    status = await sync.execute(progress)
    assert status["collection"] == stage_collection
    assert len(container.index.collections) == count_before
    assert status["count"] == 5
    assert container.embeddings.calls - calls_before == 1


async def test_startup_uses_verified_persistent_catalog_without_local_xml(
    system, tmp_path, monkeypatch
):
    """Повтор up не требует возвращать XML, когда сохранённый каталог и индекс исправны."""
    from src import cli

    container, *_ = system
    container.settings.data_directory = tmp_path / "no-xml"
    before = await container.catalog.status()
    monkeypatch.setattr(cli, "build_container", lambda _: container)
    await cli.enqueue_catalog(wait=True, configured_source=True)
    assert (await container.catalog.status())["version"] == before["version"]
    assert not await container.jobs.pending()


async def test_catalog_busy_defers_without_exhausting_real_retry_budget(system, monkeypatch):
    """Конкурирующий prune/import lock не превращается в permanent failure durable задания."""
    container, _, sync, *_ = system
    original = sync.execute

    async def busy(*args, **kwargs):
        """Моделирует занятый PostgreSQL catalog advisory lock."""
        raise MapperError("catalog_busy", "Каталог занят", 409)

    job = await container.jobs.create("admin", "busy-retry", {}, "catalog")
    monkeypatch.setattr(sync, "execute", busy)
    await container.processor.process(job["id"])
    waiting = await container.jobs.get(job["id"])
    assert waiting["state"] == "queued" and waiting["attempts"] == 0
    monkeypatch.setattr(sync, "execute", original)
    await container.processor.process(job["id"])
    assert (await container.jobs.get(job["id"]))["state"] == "complete"
