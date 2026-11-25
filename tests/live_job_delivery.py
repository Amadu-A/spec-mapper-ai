# tests/live_job_delivery.py
"""Изолированные PostgreSQL/AMQP проверки; transport fixture не вызывает VLM/каталог.

Запуск только через checked_settings в собственной временной схеме/очереди.
Потеря confirm внедряется на границе клиента после реального broker ACK.
"""

import asyncio
import json
import sys
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import update

from src.application.delivery import dispatch_once
from src.infrastructure.database import JobRow
from src.worker import maintenance
from tests.live_invoice_runtime import checked_settings, live_container


def report(**values):
    """Печатает только технические счётчики и результаты, без credentials/payload."""
    print(json.dumps(values), flush=True)


async def noop(*args):
    """Исключает scheduler/pruner из узкой transport fixture."""


async def finish(container, identifier):
    """Закрывает только синтетическое QA-задание через обычный fence/checkpoint."""
    job = await container.jobs.claim(identifier)
    assert job
    assert await container.jobs.checkpoint(identifier, job["lease_token"], state="failed")


async def stable_queue(container):
    """Повторяет прежние 660 секунд: два Unacked, три waiters, пять unique jobs."""
    published, held, leases, identifiers = [], [], [], []
    all_published = asyncio.Event()
    original_publish = container.broker.publish

    async def counted(identifier, generation=None):
        """Считает только фактически подтверждённые AMQP публикации."""
        await original_publish(identifier, generation=generation)
        published.append(identifier)
        if len(published) == 5:
            all_published.set()

    container.broker.publish = counted
    for index, kind in enumerate(["mapping", "mapping", "invoice", "catalog", "catalog_events"]):
        job = await container.jobs.create(
            "qa-delivery", "after-" + str(index), {"source_job_id": str(uuid4())}, kind
        )
        identifiers.append(job["id"])
    stub = SimpleNamespace(
        jobs=container.jobs,
        broker=container.broker,
        sync_repository=SimpleNamespace(touch=noop, cleanup=noop),
        sync=SimpleNamespace(tick=noop),
        catalog_events=None,
        catalog_sources=SimpleNamespace(cleanup=noop),
        pruner=SimpleNamespace(execute=noop),
    )
    dispatcher = asyncio.create_task(maintenance(stub))
    await asyncio.wait_for(all_published.wait(), 30)
    for _ in range(2):
        message = await container.broker.queue.get(timeout=5)
        held.append(message)
        identifier = message.body.decode("ascii")
        job = await container.jobs.claim(identifier, message.headers.get("mapper-generation"))
        assert job
        leases.append((identifier, job["lease_token"]))

    async def heartbeat():
        """Сохраняет реальные lease занятых двух слотов, как в исходном воспроизведении."""
        while True:
            for identifier, token in leases:
                assert await container.jobs.heartbeat(identifier, token)
            await asyncio.sleep(5)

    pulse = asyncio.create_task(heartbeat())
    start = monotonic()
    try:
        while monotonic() - start < 631:
            declared = await container.broker.queue.declare()
            assert declared.message_count == 3 and len(published) == 5
            report(
                scenario="after_fix",
                elapsed_seconds=round(monotonic() - start, 1),
                unique_jobs=5,
                ready=declared.message_count,
                unacked_held=2,
                publisher_confirms=len(published),
                worker_concurrency=2,
            )
            await asyncio.sleep(30)
        report(
            stable_queue_passed=True,
            elapsed_seconds=round(monotonic() - start, 1),
            ready=3,
            unique_jobs=5,
            publisher_confirms=5,
        )
    finally:
        dispatcher.cancel()
        pulse.cancel()
        await asyncio.gather(dispatcher, pulse, return_exceptions=True)
        for identifier, token in leases:
            await container.jobs.checkpoint(identifier, token, state="failed")
        for message in held:
            await message.ack()
        for _ in range(3):
            message = await container.broker.queue.get(timeout=5)
            await finish(container, message.body.decode("ascii"))
            await message.ack()
        container.broker.publish = original_publish


async def faults(container):
    """Проверяет реальные AMQP ошибки и durable receipt; VLM здесь не участвует."""
    settings = container.settings
    owner = "qa-fault-" + uuid4().hex
    original_publish = container.broker.publish
    job = await container.jobs.create(owner, "lost-confirm", {"source_job_id": str(uuid4())})
    attempt = await container.jobs.reserve_delivery()
    await original_publish(job["id"], generation=attempt["generation"])
    # Моделируем смерть клиента после broker ACK, до SQL confirm commit.
    assert await container.jobs.delivery_failure(attempt, "injected_confirm_lost")
    first = await container.broker.queue.get(timeout=5)
    receipt = await container.jobs.claim(job["id"], attempt["generation"])
    assert receipt and not await container.jobs.delivery_failure(attempt, "late_error")
    assert await container.jobs.reserve_delivery() is None
    await container.jobs.checkpoint(job["id"], receipt["lease_token"], state="failed")
    await first.ack()
    report(fault="lost_confirm_at_client_commit_boundary", recovered=True)

    # Mandatory return: только несуществующее имя в собственном namespace.
    job = await container.jobs.create(owner, "returned", {"source_job_id": str(uuid4())})
    queue_name = settings.rabbitmq_queue
    settings.rabbitmq_queue = queue_name + ".unroutable"
    assert await dispatch_once(container.jobs, container.broker) == 0
    settings.rabbitmq_queue = queue_name
    diagnostic = next(
        r for r in await container.jobs.delivery_diagnostics() if r["job_id"] == job["id"]
    )
    assert diagnostic["delivery_state"] == "pending" and diagnostic["error"]
    await asyncio.sleep(2.1)
    assert await dispatch_once(container.jobs, container.broker) == 1
    message = await container.broker.queue.get(timeout=5)
    await finish(container, job["id"])
    await message.ack()
    report(fault="mandatory_return", recovered=True)

    # Настоящий broker NACK на отдельной одноразовой QA-очереди, без shared policies.
    overflow = await container.broker.channel.declare_queue(
        queue_name + ".nack",
        auto_delete=True,
        arguments={"x-max-length": 1, "x-overflow": "reject-publish"},
    )
    settings.rabbitmq_queue = overflow.name
    await original_publish(str(uuid4()))
    try:
        await original_publish(str(uuid4()))
    except Exception:
        report(fault="broker_nack", rejected=True)
    else:
        raise AssertionError("NACK was accepted")
    finally:
        settings.rabbitmq_queue = queue_name
        await overflow.delete(if_unused=False, if_empty=False)

    # Закрывается только своё соединение: durable queued сообщение переживает reconnect.
    job = await container.jobs.create(owner, "disconnect", {"source_job_id": str(uuid4())})
    assert await dispatch_once(container.jobs, container.broker) == 1
    await container.broker.close()
    await container.broker.connect()
    message = await container.broker.queue.get(timeout=5)
    assert message.body.decode() == job["id"]
    await finish(container, job["id"])
    await message.ack()
    report(fault="own_connection_closed", durable_message_preserved=True)

    job = await container.jobs.create(
        owner, "checkpoint-before-ack", {"source_job_id": str(uuid4())}
    )
    assert await dispatch_once(container.jobs, container.broker) == 1
    message = await container.broker.queue.get(timeout=5)
    await finish(container, job["id"])
    await message.reject(requeue=True)  # Сбой после SQL terminal checkpoint, до consumer ACK.
    redelivery = await container.broker.queue.get(timeout=5)
    assert redelivery.redelivered and await container.processor.process(job["id"]) == "noop"
    await redelivery.ack()
    report(fault="checkpoint_before_ack", redelivery_noop=True)

    # Отдельный процесс погибает после durable receipt до ACK.
    job = await container.jobs.create(owner, "worker-death", {"source_job_id": str(uuid4())})
    assert await dispatch_once(container.jobs, container.broker) == 1
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.live_job_delivery",
        "receipt-worker",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(child.stdout.readline(), 20)
        assert json.loads(line)["receipt_committed"] == job["id"]
        child.kill()
        await child.wait()
        message = await container.broker.queue.get(timeout=10)
        assert message.redelivered and message.body.decode() == job["id"]
        assert (
            await container.jobs.claim(job["id"], message.headers.get("mapper-generation")) is None
        )
        await message.ack()  # Опасный ранний ACK: очередь пуста, receipt остался в SQL.
        assert await container.jobs.reserve_delivery() is None
        # Только QA lease уменьшается в SQL, production clock/настройки не меняются.
        async with container.jobs.transaction() as session:
            await session.execute(
                update(JobRow).where(JobRow.id == job["id"]).values(lease_until=0)
            )
        assert await dispatch_once(container.jobs, container.broker) == 1
        recovered = await container.broker.queue.get(timeout=5)
        assert recovered.headers["mapper-generation"] == 2
        await finish(container, job["id"])
        await recovered.ack()
        # UUID-only старый формат совместим и terminal duplicate безопасен.
        await original_publish(job["id"])
        duplicate = await container.broker.queue.get(timeout=5)
        assert await container.processor.process(job["id"]) == "noop"
        await duplicate.ack()
        report(
            fault="worker_sigkill_early_redelivery_ack",
            recovered=True,
            legacy_uuid_noop=True,
            lease_expiry="injected_only_in_qa_sql",
        )
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def run(action):
    """Валидирует namespace и закрывает только собственные соединения."""
    container = live_container(checked_settings())
    try:
        await container.broker.connect()
        if action == "receipt-worker":
            message = await container.broker.queue.get(timeout=10)
            identifier = message.body.decode()
            assert await container.jobs.claim(identifier, message.headers.get("mapper-generation"))
            report(receipt_committed=identifier)
            await asyncio.Event().wait()
        elif action == "faults":
            await faults(container)
            report(delivery_faults_passed=True)
        else:
            await stable_queue(container)
            await faults(container)
            report(delivery_live_qa_passed=True)
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else "run"))
