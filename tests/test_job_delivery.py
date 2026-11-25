# tests/test_job_delivery.py
"""Offline regression outbox/confirm/fence; реальный RabbitMQ проверяется отдельно."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.application.delivery import dispatch_once
from src.domain.models import MapperError
from src.infrastructure.broker import RabbitBroker


async def test_confirmed_waiters_do_not_republish_twenty_intervals(system, source):
    """20 прежних интервалов: published queued не превращается в новый outbox."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    published = []

    async def publish(job_id, generation=None):
        """Явный offline confirm double; реального AMQP здесь нет."""
        published.append((job_id, generation))

    for index in range(5):
        payload = deepcopy(source)
        payload["items"][0]["Caption"] += str(index)
        await container.mappings.import_document("owner", "waiter-" + str(index), payload)
    broker = SimpleNamespace(publish=publish)
    await dispatch_once(container.jobs, broker)
    await dispatch_once(container.jobs, broker)
    assert len(published) == 5
    for _ in range(21):
        clock[0] += 31
        assert await dispatch_once(container.jobs, broker) == 0
    assert len(published) == 5
    assert {item["delivery_state"] for item in await container.jobs.delivery_diagnostics()} == {
        "published"
    }


async def test_early_redelivery_ack_remains_recoverable_after_worker_death(system):
    """Receipt переживает ранний ACK; recovery выдаёт новый fence без повторного fetch."""
    container, _, _, _, extraction = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.submit("owner", "death", str(uuid4()))
    attempt = await container.jobs.reserve_delivery()
    assert await container.jobs.confirm_delivery(attempt)
    first = await container.jobs.claim(job["id"], attempt["generation"])
    assert await container.jobs.claim(job["id"], attempt["generation"]) is None
    assert await container.jobs.reserve_delivery() is None
    clock[0] += container.settings.lease_seconds + 1
    recovery = await container.jobs.reserve_delivery()
    assert recovery["generation"] == attempt["generation"] + 1
    assert not await container.jobs.checkpoint(job["id"], first["lease_token"], state="failed")
    assert not await container.jobs.confirm_delivery(attempt)
    await container.processor.process(job["id"], recovery["generation"])
    await container.processor.process(job["id"], recovery["generation"])
    assert (await container.jobs.get(job["id"]))["state"] in {"complete", "ready_for_review"}
    assert extraction.calls == 1
    diagnostics = (await container.jobs.delivery_diagnostics())[0]
    assert diagnostics["recoveries"] == 1 and diagnostics["duplicate_noops"] == 2


async def test_lost_confirm_and_stale_reservation_do_not_erase_receipt(system, source):
    """Сбой до commit допускает transport duplicate, но receipt сильнее позднего error/confirm."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.import_document("owner", "confirm", source)
    first = await container.jobs.reserve_delivery()
    assert await container.jobs.delivery_failure(first, "confirm_lost")
    assert await container.jobs.reserve_delivery() is None
    clock[0] += 61
    second = await container.jobs.reserve_delivery()
    assert second["generation"] == first["generation"] and second["token"] != first["token"]
    claimed = await container.jobs.claim(job["id"], second["generation"])
    assert claimed
    assert not await container.jobs.delivery_failure(second, "late_failure")
    assert not await container.jobs.confirm_delivery(first)
    assert await container.jobs.reserve_delivery() is None
    assert await container.jobs.checkpoint(
        job["id"], claimed["lease_token"], state="queued", stage="retry"
    )
    third = await container.jobs.reserve_delivery()
    assert third["generation"] == second["generation"] + 1
    assert not await container.jobs.confirm_delivery(second)


async def test_dispatcher_crash_and_competing_reservations(system, source):
    """Оборванная reservation ограничена lease; конкурирующие циклы не резервируют её дважды."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.import_document("owner", "reservation", source)
    results = await asyncio.gather(*(container.jobs.reserve_delivery() for _ in range(8)))
    accepted = [result for result in results if result]
    assert len(accepted) == 1 and accepted[0]["job_id"] == job["id"]
    clock[0] += container.settings.delivery_reservation_seconds + 1
    restored = await container.jobs.reserve_delivery()
    assert restored["token"] != accepted[0]["token"]
    assert not await container.jobs.confirm_delivery(accepted[0])


async def test_positive_confirm_is_required_and_mandatory_return_is_enabled(settings, monkeypatch):
    """None/NACK не публикуются успешно; on-return handling проверяется реальным client contract."""
    broker = RabbitBroker(settings)

    async def connected():
        """Отключает только сетевое соединение в offline проверке client response."""

    async def publish(*args, **kwargs):
        """Эмулирует отсутствие положительного confirm, а не успешную публикацию."""
        assert kwargs["mandatory"] is True
        return None

    monkeypatch.setattr(broker, "connect", connected)
    broker.channel = SimpleNamespace(default_exchange=SimpleNamespace(publish=publish))
    with pytest.raises(MapperError, match="не подтвердил"):
        await broker.publish(str(uuid4()), generation=1)


async def test_bounded_reconciliation_requires_explicit_eligible_ids(system, source):
    """Утрата очереди не запускает массовый timer replay; оператор указывает eligible UUID."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.import_document("owner", "reconcile", source)
    attempt = await container.jobs.reserve_delivery()
    await container.jobs.confirm_delivery(attempt)
    assert (await container.jobs.reconcile_delivery([job["id"]]))["scheduled"] == []
    clock[0] += 301
    assert await container.jobs.reserve_delivery() is None
    assert (await container.jobs.reconcile_delivery([job["id"]]))["scheduled"] == [job["id"]]
    assert (await container.jobs.reconcile_delivery([job["id"]]))["scheduled"] == []
    with pytest.raises(MapperError):
        await container.jobs.reconcile_delivery([str(uuid4()) for _ in range(11)])
