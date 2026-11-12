# src/application/catalog_events.py
"""Coalesced webhook через тот же каталогический lock/staging/alias use-case."""

from __future__ import annotations

from src.application.event_ports import CatalogEventsRepository
from src.application.ports import CatalogRepository
from src.domain.models import MapperError, ParsedCatalog, active_catalog, digest


class CatalogEvents:
    """Соединяет durable inbox, scheduler management и единственный synchronizer."""

    def __init__(
        self,
        repository: CatalogEventsRepository,
        catalog: CatalogRepository,
        sources,
        management,
        synchronizer,
        settings,
    ):
        """Получает порты через composition root; не создаёт SQL/HTTP клиентов."""
        self.repository, self.catalog, self.sources = repository, catalog, sources
        self.management, self.synchronizer, self.settings = management, synchronizer, settings

    async def tick(self):
        """Ждёт подтверждённый JSON/bootstrap и пропускает события во время full sync."""
        current = await self.management.repository.settings()
        config = self.management.config(current)
        if (
            config["source_type"] != "http_onec"
            or config["expected_format"] != "json"
            or not self.management.valid_probe(current, fresh=False)
        ):
            return None
        await self.sources.validate(config, enabling=True)
        if not (await self.catalog.status())["ready"]:
            return None
        config["_source_identity"] = self.sources.identity(config)
        backlog = await self.repository.summary()
        # При burst сначала планируется обычная полная контрольная сверка, inbox остаётся durable.
        if backlog["pending"] + backlog["failed"] >= self.settings.onec_webhook_max_backlog // 2:
            await self.management.trigger(
                "system:onec-reconcile",
                "event-backlog:" + str(int(self.management.clock() // 3600)),
                "http_onec",
            )
        return await self.repository.enqueue(config)

    async def execute(self, job: dict, progress):
        """Crash после activate повторно завершает job, не применяя уже applied события."""
        events = await self.repository.load(job)
        if all(event["state"] == "applied" for event in events):
            return await self.catalog.status()
        source = EventBatchSource(self, job, events)
        return await self.synchronizer.execute(progress, source_override=source)

    async def summary(self, journal=False):
        """Выдаёт реальные агрегаты и явный статус ожидания контракта/bootstrap."""
        result = await self.repository.summary(journal)
        configured = await self.management.settings()
        status = await self.catalog.status()
        result.update(
            state="awaiting_contract"
            if not configured["contract_confirmed"]
            else "awaiting_bootstrap"
            if not status["ready"]
            else "ready"
            if configured["source_type"] == "http_onec"
            and configured["expected_format"] == "json"
            and configured["is_configured"]
            else "awaiting_configuration",
            webhook_url=self.settings.app_public_base_url.rstrip("/") + "/api/v1/catalog/events",
            secret_configured=bool(self.settings.onec_catalog_webhook_key.get_secret_value()),
            batch_window_seconds=self.settings.onec_webhook_batch_window_seconds,
        )
        return result


class EventBatchSource:
    """Получает только затронутые GUID; публикацию выполняет общий SynchronizeCatalogUseCase."""

    def __init__(self, service: CatalogEvents, job: dict, events: list[dict]):
        """Фиксирует пачку и текущий fence до входа в общий каталогический lock."""
        self.service, self.job, self.events = service, job, events
        self.config = job["data"]["catalog_sync"]
        self.changes = {}
        for event in sorted(events, key=lambda e: (e["occurred_at"], e["event_id"])):
            for change in event["changes"]:
                self.changes[change["guid"]] = {**change, "occurred_at": event["occurred_at"]}
        self.observed_hash = ""

    async def current(self) -> dict:
        """Повторно читает актуальные карточки: stale upsert/delete не переносит старые поля."""
        found, guids = {}, sorted(self.changes)
        size = self.service.settings.onec_webhook_batch_size
        for start in range(0, len(guids), size):
            for item in await self.service.sources.batch(self.config, guids[start : start + size]):
                if item.onec_guid in found:
                    raise MapperError("duplicate_guid", "Batch повторил настоящий GUID", 503)
                found[item.onec_guid] = item
        return found

    async def read(self) -> ParsedCatalog:
        """Создаёт один snapshot на пачку, не принимая отсутствие batch-записи за удаление."""
        service = self.service
        status = await service.catalog.status()
        if not status["ready"]:
            raise MapperError(
                "catalog_not_ready", "Webhook ожидает первоначальный полный импорт", 503
            )
        # Проверка fence выполняется и после ожидания общего lock полного импорта.
        await service.repository.load(self.job)
        existing = await service.catalog.items(
            status["version"], await service.catalog.guids(status["version"])
        )
        identities = await service.repository.identities()
        found = await self.current()
        self.observed_hash = digest({guid: item.to_dict() for guid, item in found.items()})
        tombstones = {}
        for guid, change in self.changes.items():
            binding = identities.get(guid, {})
            internal = binding.get("guid", guid)
            if guid in found:
                existing.pop(internal, None)
                existing[guid] = found[guid]
            elif change["action"] == "delete":
                existing.pop(internal, None)
                tombstones[guid] = change["occurred_at"]
            elif (binding.get("tombstone_at") or 0) < change["occurred_at"]:
                raise MapperError(
                    "onec_batch_missing",
                    "Upsert GUID отсутствует в batch; требуется повтор или полная сверка",
                    503,
                )
        records = list(existing.values())
        items, warnings, stats = active_catalog(records)
        return ParsedCatalog(
            items,
            digest([status["version"], sorted(e["id"] for e in self.events), self.observed_hash]),
            warnings,
            stats,
            records,
            {
                "event_context": {
                    "job_id": self.job["id"],
                    "lease_token": self.job["lease_token"],
                    "event_ids": [e["id"] for e in self.events],
                    "tombstones": tombstones,
                }
            },
        )

    async def before_publish(self):
        """Перечитывает batch до alias switch и выявляет изменения 1С во время staging."""
        await self.service.repository.load(self.job)
        latest = await self.current()
        if digest({guid: item.to_dict() for guid, item in latest.items()}) != self.observed_hash:
            raise MapperError(
                "onec_batch_changed",
                "Карточки изменились во время staging; пачка будет перечитана",
                503,
            )
