# tests/test_recovery.py
"""Критические отказные сценарии: retry, resume без повторного fetch и уборка старых snapshots."""

from time import time
from uuid import uuid4

import pytest
from sqlalchemy import update

from src.domain.models import MapperError
from src.infrastructure.database import CatalogVersionRow


async def test_retry_resumes_source_without_downloading_again(system, monkeypatch):
    """Временный отказ поиска после fetching не скачивает source повторно на следующей доставке."""
    container, _, _, _, extraction = system
    container.settings.max_attempts = 2
    job = await container.mappings.submit("owner", "retry", str(uuid4()))
    original_search = container.index.search
    calls = 0

    async def failing_search(*args):
        """Отклоняет только первый сетевой поиск как восстанавливаемый отказ зависимости."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MapperError("qdrant_unavailable", "Временный отказ", 503)
        return await original_search(*args)

    monkeypatch.setattr(container.index, "search", failing_search)
    await container.processor.process(job["id"])
    pending = await container.jobs.get(job["id"])
    assert pending["state"] == "queued" and pending["data"]["source"]
    assert pending["error_code"] == "qdrant_unavailable"
    await container.processor.process(job["id"])
    ready = await container.mappings.result("owner", job["id"])
    assert len(ready["data"]["rows"]) == 2
    assert ready["error_code"] is None
    assert extraction.calls == 1


async def test_model_change_is_explicit(system):
    """Несовместимая модель даёт failed вместо пустого успешного результата."""
    container, *_ = system
    container.settings.max_attempts = 1
    container.embeddings.fingerprint = "changed"
    job = await container.mappings.submit("owner", "incompatible", str(uuid4()))
    await container.processor.process(job["id"])
    failed = await container.jobs.get(job["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "embedding_incompatible"


async def test_catalog_job_and_fresh_manual_selection_reject_retired_item(system, source):
    """Фоновый импорт проходит state machine; удалённая позиция не выбирается заново."""
    container, item, _, catalog_source, _ = system
    job = await container.mappings.import_document("owner", "m", source)
    await container.processor.process(job["id"])
    result = await container.jobs.get(job["id"])
    from dataclasses import replace

    catalog_source.items = [replace(item, guid=str(uuid4()), code="new", name="Новая позиция")]
    catalog_source.source_hash = "new"
    imported = await container.jobs.create("system", "catalog", {}, "catalog")
    await container.processor.process(imported["id"])
    assert (await container.jobs.get(imported["id"]))["state"] == "complete"
    status = await container.catalog.status()
    assert status["removed_count"] == 1
    assert item.guid not in container.index.collections[status["collection"]]
    with pytest.raises(MapperError) as error:
        await container.mappings.select("owner", job["id"], 0, item.guid, result["revision"])
    assert error.value.code == "catalog_item_unavailable"


async def test_pruner_keeps_active_previous_and_live_job_snapshot(system, source):
    """Удаляет только obsolete staged версии; active, rollback и живые job refs защищены."""
    container, _, sync, catalog_source, _ = system

    async def progress(stage):
        """Пропускает standalone этапы controlled синхронизации в integration test."""
        return None

    first = await container.catalog.status()
    job = await container.mappings.import_document("owner", "pin", source)
    await container.processor.process(job["id"])
    for marker in ("second", "third"):
        catalog_source.source_hash = marker
        await sync.execute(progress)
    async with container.jobs.sessions.begin() as session:
        await session.execute(update(CatalogVersionRow).values(created_at=time() - 7200))
    assert not await container.catalog.obsolete_versions()
    await container.pruner.execute()
    assert first["collection"] in container.index.collections
    container.jobs.clock = lambda: time() + 100000
    await container.jobs.cleanup()
    obsolete = await container.catalog.obsolete_versions()
    assert [row["version"] for row in obsolete] == [first["version"]]
    await container.pruner.execute()
    assert first["collection"] not in container.index.collections
    assert (await container.catalog.status())["ready"]
