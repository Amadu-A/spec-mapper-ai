# tests/test_catalog_events.py
"""Offline webhook: durable commit, batch/debounce, fencing, retry и общий staging pipeline."""

import json
from datetime import UTC, datetime
from time import time
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_catalog_json import card, progress

from src.domain.catalog_sync import sync_defaults
from src.domain.models import MapperError
from src.infrastructure.database import CatalogEventRow
from src.main import create_app


@pytest.fixture
async def onec_system(system, tmp_path):
    """Использует SQLite/FakeIndex и изолированный HTTPS mock 1С, без live утверждений."""
    container, item, sync, *_ = system
    settings = container.settings
    settings.onec_catalog_contract_confirmed = (
        True  # Только contract mock, production env неизменен.
    )
    settings.onec_catalog_allowed_hosts = ["onec.mock"]
    settings.onec_catalog_allowed_networks = ["10.250.0.0/24"]
    settings.onec_catalog_token = SecretStr("mock-only-outbound-token")
    settings.onec_catalog_webhook_key = SecretStr("w" * 32)
    settings.catalog_staging_directory = tmp_path / "spool"
    clock = [time()]
    container.jobs.clock = lambda: clock[0]
    record = card(code=item.code, name=item.name, full_name=item.name, article="")
    records, calls = {record["guid"]: record}, []
    fault = {"status": 200, "missing": False}

    async def resolve(host, port):
        """DNS mock закрепляет единственный разрешённый адрес без сети."""
        return ["10.250.0.20"]

    def handle(request):
        """Предоставляет реальный JSON контракт export/batch через httpx MockTransport."""
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"items": list(records.values())}, headers={"X-Catalog-Complete": "true"}
            )
        ids = json.loads(request.content)["guids"]
        assert len(ids) <= 50
        return httpx.Response(
            fault["status"],
            json={
                "items": []
                if fault["missing"]
                else [records[guid] for guid in ids if guid in records]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    factory = container.catalog_sources
    factory.client, factory.resolver = client, resolve
    config = sync_defaults(settings) | {
        "source_type": "http_onec",
        "expected_format": "json",
        "url": "https://onec.mock/catalog/export",
        "auth_type": "bearer",
        "completion_header": "X-Catalog-Complete",
        "completion_value": "true",
    }
    current = await container.sync.settings()
    await container.sync.save(config, current["revision"], "test-operator")
    await container.sync.connection_test()
    await sync.execute(progress, factory.source(config, progress))
    container.catalog_events.synchronizer = sync
    container.processor.catalog_events = container.catalog_events
    yield container, records, calls, clock, fault
    await client.aclose()


def event_payload(changes, occurred=None, event_id=None):
    """Создаёт синтетическое событие с UTC временем и стабильным UUID."""
    return {
        "event_id": event_id or str(uuid4()),
        "occurred_at": occurred or datetime.now(UTC).isoformat(),
        "changes": changes,
    }


async def accept(container, changes, occurred=None):
    """Проходит настоящий API webhook и возвращает durable идентификатор."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/v1/catalog/events",
            json=event_payload(changes, occurred),
            headers={"X-Webhook-Key": "w" * 32},
        )
        assert result.status_code == 202, result.text
        return result.json()


async def run_batch(container, clock):
    """Переводит только тестовые часы debounce, выполняет настоящее durable задание."""
    clock[0] += 31
    job = await container.catalog_events.tick()
    assert job and job["kind"] == "catalog_events"
    await container.processor.process(job["id"])
    return await container.jobs.get(job["id"])


async def test_webhook_commit_dedup_conflict_auth_and_openapi(onec_system, monkeypatch):
    """202 после SQL commit без обращения к внешним системам, отдельный ключ и conflict 409."""
    container, records, calls, *_ = onec_system
    calls.clear()
    payload = event_payload([{"guid": next(iter(records)), "action": "upsert"}])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        headers = {"X-Webhook-Key": "w" * 32}
        assert (
            await client.post(
                "/api/v1/catalog/events", json=payload, headers={"X-API-Key": "a" * 32}
            )
        ).status_code == 401
        assert (
            await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        ).status_code == 202
        async with container.catalog.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CatalogEventRow)) == 1
        replay = await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json()["duplicate"]
        payload["changes"][0]["action"] = "delete"
        assert (
            await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        ).status_code == 409
        assert not calls
        schema = (await client.get("/api/openapi.json")).json()
        assert (
            schema["components"]["securitySchemes"]["CatalogWebhookKey"]["name"] == "X-Webhook-Key"
        )
        public = await client.get("/api/v1/catalog/event-status", headers={"X-API-Key": "a" * 32})
        assert public.status_code == 200 and "items" not in public.json()
        assert (
            await client.get("/api/v1/catalog/events", headers={"X-API-Key": "a" * 32})
        ).status_code == 403
        assert "w" * 32 not in public.text


async def test_webhook_limits_ip_and_failed_durable_commit(onec_system, monkeypatch):
    """Ограничения тела/изменений/частоты независимы от пользовательского refresh."""
    container, records, *_ = onec_system
    payload = event_payload([{"guid": next(iter(records)), "action": "upsert"}])
    headers = {"X-Webhook-Key": "w" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        too_many = event_payload([{"guid": str(uuid4()), "action": "upsert"} for _ in range(101)])
        assert (
            await client.post("/api/v1/catalog/events", json=too_many, headers=headers)
        ).status_code == 422
        assert (
            await client.post("/api/v1/catalog/events", content=b"x" * 32769, headers=headers)
        ).status_code == 413
        container.settings.onec_webhook_allowed_networks = ["10.250.0.0/24"]
        assert (
            await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        ).status_code == 403
        container.settings.onec_webhook_allowed_networks = []

        async def unavailable(event):
            """Моделирует SQL commit failure: API обязан просить повтор, а не отвечать 202."""
            raise OSError("mock DB failure")

        monkeypatch.setattr(container.catalog_events.repository, "receive", unavailable)
        failed = await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        assert failed.status_code == 503 and failed.headers["retry-after"] == "5"
    container.settings.onec_webhook_rate_limit_per_minute = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        assert (
            await client.post("/api/v1/catalog/events", json=payload, headers=headers)
        ).status_code == 429
        assert (await client.get("/health/live")).status_code == 200


async def test_debounce_coalesces_and_updates_only_semantic_vectors(onec_system):
    """Много событий одного GUID создают один snapshot; metadata-only не вызывает embedding."""
    container, records, calls, clock, _ = onec_system
    guid = next(iter(records))
    calls.clear()
    before = await container.catalog.status()
    embeddings = container.embeddings.calls
    for _ in range(6):
        await accept(container, [{"guid": guid, "action": "upsert"}])
    assert await container.catalog_events.tick() is None
    records[guid]["brand"] = "ROSMA"
    result = await run_batch(container, clock)
    assert result["state"] == "complete", result
    after = await container.catalog.status()
    assert after["version"] != before["version"] and container.embeddings.calls == embeddings + 1
    summary = await container.catalog_events.summary(True)
    assert summary["applied"] == 6 and summary["failed"] == summary["pending"] == 0
    assert len([request for request in calls if request.method == "POST"]) == 2
    records[guid]["code"] = "000000007"
    await accept(container, [{"guid": guid, "action": "upsert"}])
    await run_batch(container, clock)
    assert container.embeddings.calls == embeddings + 1
    stable = await container.catalog.guids(after["version"])
    assert await container.catalog.guids((await container.catalog.status())["version"]) == stable


async def test_upsert_delete_tombstone_stale_and_missing_batch(onec_system):
    """Missing upsert не удаляет; delete идемпотентен; старый delete не стирает текущую карточку."""
    container, records, _, clock, fault = onec_system
    guid = next(iter(records))
    before = await container.catalog.status()
    await accept(container, [{"guid": guid, "action": "upsert"}])
    fault["missing"] = True
    job = await run_batch(container, clock)
    assert job["error_code"] == "onec_batch_missing"
    assert (await container.catalog.status())["version"] == before["version"]
    fault["missing"] = False
    # Тот же job повторяет durable checkpoint с тем же inbox, не создавая новый event.
    await container.processor.process(job["id"])
    assert (await container.jobs.get(job["id"]))["state"] == "complete"
    await accept(container, [{"guid": guid, "action": "delete"}])
    await run_batch(container, clock)
    assert (await container.catalog.status())["count"] == 1  # Карточка существует: stale delete.
    records.pop(guid)
    await accept(container, [{"guid": guid, "action": "delete"}])
    deleted = await run_batch(container, clock)
    assert deleted["state"] == "complete" and (await container.catalog.status())["count"] == 0
    await accept(container, [{"guid": guid, "action": "delete"}])
    assert (await run_batch(container, clock))["state"] == "complete"
    await accept(container, [{"guid": guid, "action": "upsert"}], occurred="2020-01-01T00:00:00Z")
    assert (await run_batch(container, clock))["state"] == "complete"
    assert (await container.catalog.status())["count"] == 0


async def test_deleted_marker_new_item_and_batch_split(onec_system):
    """Обновляет больше 50 GUID ограниченными batch, deleted=true не индексируется."""
    container, records, calls, clock, _ = onec_system
    old = next(iter(records))
    records[old]["deleted"] = True
    fresh = [card(code=f"{i:08}") for i in range(70)]
    records.update({row["guid"]: row for row in fresh})
    calls.clear()
    await accept(
        container,
        [{"guid": row["guid"], "action": "upsert"} for row in fresh]
        + [{"guid": old, "action": "upsert"}],
    )
    assert (await run_batch(container, clock))["state"] == "complete"
    status = await container.catalog.status()
    assert status["count"] == 70 and await container.index.count(status["collection"]) == 70
    assert len(calls) == 4  # Две пачки чтения + две проверки перед publish.


async def test_publish_failure_retry_fence_and_event_atomicity(onec_system, monkeypatch):
    """Отказ SQL после alias восстанавливает прежний индекс; replay завершает ровно одну пачку."""
    container, records, _, clock, _ = onec_system
    guid = next(iter(records))
    records[guid]["article"] = "000006836"
    await accept(container, [{"guid": guid, "action": "upsert"}])
    clock[0] += 31
    job = await container.catalog_events.tick()
    assert job["id"] in await container.jobs.pending()
    first = await container.jobs.claim(job["id"])
    clock[0] += container.settings.lease_seconds + 1
    second = await container.jobs.claim(job["id"])
    with pytest.raises(MapperError):
        await container.catalog_events.repository.load(first)
    assert first["lease_token"] != second["lease_token"]
    original = container.catalog.activate
    before = await container.catalog.status()
    old_alias = container.index.alias

    async def failed(version):
        """Моделирует отказ PG перед commit после переключения alias."""
        raise MapperError("mock_commit_failure", "Mock PG failure", 503)

    monkeypatch.setattr(container.catalog, "activate", failed)
    with pytest.raises(MapperError):
        await container.catalog_events.execute(second, progress)
    assert container.index.alias == old_alias
    assert (await container.catalog_events.summary())["applied"] == 0
    assert (await container.catalog.status())["version"] == before["version"]
    monkeypatch.setattr(container.catalog, "activate", original)
    await container.catalog_events.execute(second, progress)
    assert (await container.catalog_events.summary())["applied"] == 1
    published = (await container.catalog.status())["version"]
    await container.catalog_events.execute(second, progress)  # Crash до terminal checkpoint.
    assert (await container.catalog.status())["version"] == published


async def test_full_sync_accumulates_events_and_checks_current_before_publish(
    onec_system, monkeypatch
):
    """Общий lock не допускает два индексатора; изменение batch до publish запускает retry."""
    container, records, _, clock, _ = onec_system
    guid = next(iter(records))
    await accept(container, [{"guid": guid, "action": "upsert"}])
    clock[0] += 31
    config = container.sync.config(await container.sync.repository.settings())
    full = await container.sync.repository.trigger("operator", "full-concurrent", config, "manual")
    assert await container.catalog_events.tick() is None
    original_source = container.catalog_sources.source

    def during_full(configuration, callback):
        """Событие приходит после начала download: full не вправе засчитать его заранее."""
        original = original_source(configuration, callback)

        class DuringFull:
            """Подменяет только момент появления второго события в mock full download."""

            async def read(self):
                """Возвращает прежний snapshot и независимо фиксирует входящее событие."""
                parsed = await original.read()
                await accept(container, [{"guid": guid, "action": "upsert"}])
                return parsed

        return DuringFull()

    monkeypatch.setattr(container.catalog_sources, "source", during_full)
    await container.processor.process(full["id"])
    monkeypatch.setattr(container.catalog_sources, "source", original_source)
    clock[0] += 31
    job = await container.catalog_events.tick()
    assert job
    claimed = await container.jobs.claim(job["id"])
    original = container.index.clone

    async def mutate(source, target):
        """Карточка меняется после первого batch, но до окончательной проверки 1С."""
        await original(source, target)
        records[guid]["brand"] = "Changed during staging"

    monkeypatch.setattr(container.index, "clone", mutate)
    before = await container.catalog.status()
    with pytest.raises(MapperError) as error:
        await container.catalog_events.execute(claimed, progress)
    assert error.value.code == "onec_batch_changed"
    assert (await container.catalog.status())["version"] == before["version"]
    assert (await container.catalog_events.summary())["applied"] == 1


async def test_configured_http_startup_without_xml_and_unchanged_keys(
    onec_system, tmp_path, monkeypatch
):
    """Единая команда bootstrap использует SQL HTTP источник и не требует XML."""
    from src import cli

    container, *_ = onec_system
    container.settings.data_directory = tmp_path / "no-xml"
    monkeypatch.setattr(cli, "build_container", lambda _: container)
    published = []

    async def publish(job_id):
        """Доставляет настоящий outbox в processor без mock успешного terminal ответа."""
        published.append(job_id)
        await container.processor.process(job_id)

    monkeypatch.setattr(container.broker, "publish", publish)
    before = container.settings.onec_catalog_webhook_key.get_secret_value()
    await cli.enqueue_catalog(wait=True, configured_source=True)
    assert len(published) == 1
    assert (await container.jobs.get(published[0]))["state"] == "complete"
    assert container.settings.onec_catalog_webhook_key.get_secret_value() == before


async def test_webhook_does_not_consume_due_full_schedule_slot(onec_system):
    """Активная webhook пачка задерживает daily full, но не помечает UTC слот выполненным."""
    container, records, _, clock, _ = onec_system
    config = container.sync.config(await container.sync.repository.settings())
    current = await container.sync.settings()
    config["enabled"] = True
    saved = await container.sync.save(config, current["revision"], "operator")
    await accept(container, [{"guid": next(iter(records)), "action": "upsert"}])
    clock[0] += 31
    batch = await container.catalog_events.tick()
    assert batch
    clock[0] = saved["next_run_at"] + 1
    assert await container.sync.tick() is None
    assert (await container.sync.settings())["next_run_at"] == saved["next_run_at"]
    await container.processor.process(batch["id"])
    full = await container.sync.tick()
    assert full and full["kind"] == "catalog"
    assert (await container.sync.settings())["next_run_at"] > clock[0]


async def test_event_quarantine_survives_omitted_cards_and_unknown_delete_tombstone(onec_system):
    """Повторный delta не возвращает code conflicts в подбор; неизвестный delete сохраняется."""
    container, records, _, clock, _ = onec_system
    fresh = [card(code="CONFLICT") for _ in range(2)]
    records.update({row["guid"]: row for row in fresh})
    await accept(container, [{"guid": row["guid"], "action": "upsert"} for row in fresh])
    assert (await run_batch(container, clock))["state"] == "complete"
    assert (await container.catalog.status())["count"] == 1
    third = card(code="CONFLICT")
    records[third["guid"]] = third
    await accept(container, [{"guid": third["guid"], "action": "upsert"}])
    assert (await run_batch(container, clock))["state"] == "complete"
    status = await container.catalog.status()
    assert status["count"] == 1 and status["statistics"]["quarantined_rows"] == 3
    assert not await container.catalog.search("CONFLICT", 20)
    unknown = str(uuid4())
    await accept(container, [{"guid": unknown, "action": "delete"}])
    assert (await run_batch(container, clock))["state"] == "complete"
    identities = await container.catalog_events.repository.identities()
    assert identities[unknown]["tombstone_at"]
    await accept(
        container, [{"guid": unknown, "action": "upsert"}], occurred="2020-01-01T00:00:00Z"
    )
    assert (await run_batch(container, clock))["state"] == "complete"


async def test_verified_unchanged_full_snapshot_reconciles_missing_preexisting_event(onec_system):
    """Даже неизменённая полная сверка закрывает старый missing upsert по доказанной полноте."""
    container, _, _, clock, _ = onec_system
    unknown = str(uuid4())
    await accept(container, [{"guid": unknown, "action": "upsert"}])
    before = await container.catalog.status()
    config = container.sync.config(await container.sync.repository.settings())
    full = await container.sync.repository.trigger(
        "operator", "full-reconcile-missing", config, "manual"
    )
    await container.processor.process(full["id"])
    assert (await container.jobs.get(full["id"]))["state"] == "complete"
    assert (await container.catalog.status())["version"] == before["version"]
    summary = await container.catalog_events.summary()
    assert summary["applied"] == 1 and summary["pending"] == summary["failed"] == 0
    assert (await container.catalog_events.repository.identities())[unknown]["tombstone_at"]
    clock[0] += 31
    assert await container.catalog_events.tick() is None


async def test_published_event_snapshot_can_be_restored_without_reviving_old_fence(onec_system):
    """Rollback опубликованной версии не переиспользует истёкшую lease и не меняет dedup."""
    container, records, _, clock, _ = onec_system
    guid = next(iter(records))
    records[guid]["brand"] = "First"
    await accept(container, [{"guid": guid, "action": "upsert"}])
    await run_batch(container, clock)
    previous = await container.catalog.status()
    records[guid]["brand"] = "Second"
    await accept(container, [{"guid": guid, "action": "upsert"}])
    await run_batch(container, clock)
    applied = (await container.catalog_events.summary())["applied"]
    async with container.catalog.lock():
        async with container.catalog.guard():
            await container.index.switch(previous["collection"])
            await container.catalog.activate(previous["version"])
    assert (await container.catalog.status())["version"] == previous["version"]
    assert (await container.catalog_events.summary())["applied"] == applied
