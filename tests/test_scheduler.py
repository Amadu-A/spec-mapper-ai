# tests/test_scheduler.py
"""Time-travel scheduler: фиксированный UTC, restart, coalescing и сохранение каталога."""

import asyncio
from datetime import UTC, datetime

import pytest

from src.domain.catalog_sync import due_occurrence, next_occurrence, validate_time
from src.domain.models import MapperError
from src.infrastructure.sync_repository import SqlSyncRepository


def stamp(value):
    """Переводит явное UTC время в подменяемые часы scheduler."""
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


@pytest.mark.parametrize("value", ["24:00", "15:60", "3:00", "15:00Z", "15:00:00", ""])
def test_invalid_time(value):
    """Неоднозначное локальное время не принимается."""
    with pytest.raises(MapperError):
        validate_time(value)


def test_calendar_boundaries():
    """Следующий слот строго будущий, последний пропущенный не порождает backlog."""
    assert next_occurrence(stamp("2026-11-06T14:59"), "15:00") == stamp("2026-11-06T15:00")
    assert next_occurrence(stamp("2026-11-06T15:00"), "15:00") == stamp("2026-11-07T15:00")
    assert due_occurrence(stamp("2026-11-08T00:00"), "15:00") == stamp("2026-11-07T15:00")


async def enable(container, clock):
    """Сохраняет DB расписание до слота, не создавая задания при сохранении."""
    container.jobs.clock = lambda: clock[0]
    value = await container.sync_repository.settings()
    config = container.sync.config(value)
    config["enabled"] = True
    await container.sync.save(config, value["revision"], "operator")


async def test_exact_time_once_restart_and_manual_no_shift(system):
    """14:59 без запуска, 15:00 один job, 15:01/restart без дубля; manual не сдвигает слот."""
    container, *_ = system
    clock = [stamp("2026-11-06T14:59")]
    await enable(container, clock)
    assert await container.sync.tick() is None
    clock[0] = stamp("2026-11-06T15:00")
    first = await container.sync.tick()
    assert first
    await container.processor.process(first["id"])
    clock[0] += 60
    assert await container.sync.tick() is None
    other = SqlSyncRepository(
        container.sync_repository.sessions, container.settings, container.jobs
    )
    assert await other.schedule(container.catalog_sources.identity(await other.settings())) is None
    before = (await other.settings())["next_run_at"]
    manual = await container.sync.trigger("operator", "manual", "local_xml")
    assert (await other.settings())["next_run_at"] == before
    await container.processor.process(manual["id"])
    assert (await other.settings())["next_run_at"] == stamp("2026-11-07T15:00")


async def test_catchup_latest_only_and_disabled_no_fetch(system):
    """Несколько дней простоя дают один catch-up; выключение не отменяет начатое задание."""
    container, *_ = system
    clock = [stamp("2026-11-06T14:59")]
    await enable(container, clock)
    clock[0] = stamp("2026-11-10T16:00")
    job = await container.sync.tick()
    assert job
    assert await container.sync.tick() is None
    config = await container.sync_repository.settings()
    safe = container.sync.config(config)
    safe["enabled"] = False
    await container.sync.save(safe, config["revision"], "operator")
    assert await container.sync.tick() is None
    assert (await container.jobs.get(job["id"], "system"))["state"] == "queued"
    assert (await container.sync_repository.history())["items"][0][
        "initiator"
    ] == "scheduled_catch_up"


async def test_manual_at_slot_coalesces_and_concurrent_tick(system):
    """Одновременные tick одного процесса и manual объединяются с одним durable job."""
    container, *_ = system
    clock = [stamp("2026-11-06T14:59")]
    await enable(container, clock)
    manual = await container.sync.trigger("operator", "at-slot", "local_xml")
    clock[0] = stamp("2026-11-06T15:00")
    results = await asyncio.gather(*(container.sync.tick() for _ in range(3)))
    created = [value for value in results if value]
    assert len(created) == 1 and created[0]["id"] == manual["id"]
    assert len(await container.jobs.pending()) == 1
    assert (await container.sync_repository.history())["total"] == 2
    with pytest.raises(MapperError) as error:
        await container.sync.trigger("operator", "duplicate", "local_xml")
    assert error.value.code == "catalog_busy"


async def test_settings_survive_env_change(system):
    """После bootstrap окружение не заменяет сохранённые GUI настройки."""
    container, *_ = system
    original = await container.sync_repository.settings()
    container.settings.catalog_daily_sync = True
    container.settings.catalog_sync_time_utc = "09:00"
    persisted = await container.sync_repository.settings()
    assert persisted["enabled"] == original["enabled"] and persisted["time_utc"] == "15:00"
