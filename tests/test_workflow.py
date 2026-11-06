# tests/test_workflow.py
"""Функциональные и регрессионные проверки durable pipeline на настоящих repository adapters."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from src.domain.models import MapperError


async def nothing(stage):
    """Принимает progress standalone импорта без создания лишнего durable задания."""
    return None


async def test_idempotency_conflict_owner_isolation(system):
    """Повтор того же submit возвращает тот же UUID; изменённый payload и чужой
    owner отвергаются.
    """
    container, *_ = system
    source_id = str(uuid4())
    first = await container.mappings.submit("owner1", "key", source_id)
    second = await container.mappings.submit("owner1", "key", source_id)
    assert first["id"] == second["id"]
    with pytest.raises(MapperError) as error:
        await container.mappings.submit("owner1", "key", str(uuid4()))
    assert error.value.status == 409
    with pytest.raises(MapperError) as error:
        await container.jobs.get(first["id"], "owner2")
    assert error.value.status == 404


async def test_mapping_duplicates_manual_revision_and_reconnect(system, source):
    """Сопоставляет дубли, сохраняет ручной выбор и не меняет source после повторной доставки."""
    container, item, _, _, extraction = system
    original = deepcopy(source)
    job = await container.mappings.submit("owner", "k", str(uuid4()))
    await container.processor.process(job["id"])
    result = await container.mappings.result("owner", job["id"])
    assert len(result["data"]["rows"]) == 2
    assert result["data"]["source"] == original
    assert extraction.calls == 1
    updated = await container.mappings.select("owner", job["id"], 0, item.guid, result["revision"])
    assert updated["data"]["rows"][0]["confirmed"]
    assert updated["data"]["rows"][0]["status"] == "manual"
    with pytest.raises(MapperError) as error:
        await container.mappings.select("owner", job["id"], 0, None, result["revision"])
    assert error.value.code == "revision_conflict"
    await container.processor.process(job["id"])
    reconnected = await container.jobs.get(job["id"], "owner")
    assert reconnected["data"]["source"] == original
    assert reconnected["data"]["rows"][0]["selection"]["guid"] == item.guid
    assert extraction.calls == 1


async def test_lease_fencing_recovery_and_ttl_cleanup(system, source):
    """Старый fence не перезаписывает новый; TTL запрещает чтение до физической очистки каталога."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.import_document("owner", "k", source)
    first = await container.jobs.claim(job["id"])
    assert await container.jobs.claim(job["id"]) is None
    clock[0] += container.settings.lease_seconds + 1
    assert job["id"] in await container.jobs.pending()
    second = await container.jobs.claim(job["id"])
    assert first["lease_token"] != second["lease_token"]
    assert not await container.jobs.checkpoint(job["id"], first["lease_token"], stage="bad")
    assert not await container.jobs.heartbeat(job["id"], first["lease_token"])
    assert await container.jobs.checkpoint(job["id"], second["lease_token"], state="failed")
    clock[0] += container.settings.retention_seconds + 1
    with pytest.raises(MapperError) as error:
        await container.jobs.get(job["id"], "owner")
    assert error.value.status == 410
    await container.jobs.cleanup()
    assert (await container.catalog.status())["ready"]


async def test_incremental_sync_reuses_vectors_and_keeps_manual_snapshot(system, source):
    """Неизменённые позиции не векторизуются; новый каталог не переприсваивает ручной выбор."""
    container, item, sync, catalog_source, _ = system
    original_calls = container.embeddings.calls
    first = await sync.execute(nothing)
    assert container.embeddings.calls == original_calls
    job = await container.mappings.import_document("owner", "k", source)
    await container.processor.process(job["id"])
    result = await container.jobs.get(job["id"])
    await container.mappings.select("owner", job["id"], 0, item.guid, result["revision"])
    before = container.embeddings.calls
    catalog_source.items = [item, replace(item, guid=str(uuid4()), code="02", name="Новый насос")]
    catalog_source.source_hash = "xml2"
    second = await sync.execute(nothing)
    assert first["version"] != second["version"]
    assert second["embedded_count"] == 1 and second["reused_count"] == 1
    assert second["indexed_count"] == 1
    assert second["mode"] == "incremental"
    assert container.embeddings.calls == before + 1
    assert (await container.jobs.get(job["id"]))["data"]["rows"][0]["selection"][
        "name"
    ] == item.name


async def test_reindex_count_failure_keeps_active_index(system):
    """Неверный count staging не выключает старый PostgreSQL pointer или alias."""
    container, item, sync, catalog_source, _ = system
    before = await container.catalog.status()
    old_alias = container.index.alias
    catalog_source.source_hash = "changed"
    container.index.bad_count = True
    with pytest.raises(MapperError) as error:
        await sync.execute(nothing)
    assert error.value.code == "index_count"
    assert container.index.alias == old_alias
    assert (await container.catalog.status())["version"] == before["version"]


async def test_sql_commit_failure_rolls_alias_back(system, monkeypatch):
    """Если БД не фиксирует указатель, alias возвращается на предыдущую проверенную коллекцию."""
    container, _, sync, catalog_source, _ = system
    old_alias = container.index.alias
    catalog_source.source_hash = "changed"

    async def failed_activate(version):
        """Моделирует отказ транзакции активации после успешной проверки count."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(container.catalog, "activate", failed_activate)
    with pytest.raises(RuntimeError):
        await sync.execute(nothing)
    assert container.index.alias == old_alias
