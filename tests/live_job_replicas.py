# tests/live_job_replicas.py
"""Реальная конкуренция PostgreSQL replicas и admission только в собственной QA schema.

Запускается при остановленном QA worker. Production не останавливается.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from src.application.delivery import dispatch_once
from src.domain.models import MapperError
from src.infrastructure.repositories import SqlJobs
from tests.live_invoice_runtime import checked_settings, live_container


async def run():
    """Разные instances/locks конкурируют через реальный PostgreSQL, затем реальный AMQP."""
    container = live_container(checked_settings())
    try:
        replicas = [SqlJobs(container.jobs.sessions, container.settings) for _ in range(10)]
        payload = {
            "document": {
                "items": [
                    {
                        "Caption": "Тестовый клапан DN50",
                        "Units": "шт",
                        "Quantity": "1",
                        "UserComment1": "QA",
                        "Pages": "1",
                    }
                ]
            }
        }
        owner = "qa-replicas-" + uuid4().hex
        results = await asyncio.gather(
            *(
                repository.create(owner, "key-" + str(index), payload)
                for index, repository in enumerate(replicas)
            )
        )
        assert len({result["id"] for result in results}) == 1
        assert sum(not result["reused"] for result in results) == 1
        attempts = await asyncio.gather(*(r.reserve_delivery() for r in replicas))
        accepted = [r for r in attempts if r]
        assert len(accepted) == 1 and accepted[0]["job_id"] == results[0]["id"]
        await container.broker.publish(accepted[0]["job_id"], accepted[0]["generation"])
        assert await container.jobs.confirm_delivery(accepted[0])
        message = await container.broker.queue.get(timeout=5)
        identifier = message.body.decode()
        assert await container.processor.process(identifier, message.headers["mapper-generation"])
        await message.ack()
        current = await container.jobs.get(identifier)
        assert current["state"] in {"complete", "ready_for_review"}

        # Таймаут publish оставляет durable pending, а не потерянный accepted job.
        pending = await container.jobs.create(owner, "timeout", {"source_job_id": str(uuid4())})

        async def timeout(*args, **kwargs):
            """Fault injection на клиентской границе, без остановки shared RabbitMQ."""
            raise TimeoutError

        assert await dispatch_once(container.jobs, SimpleNamespace(publish=timeout)) == 0
        diagnostics = await container.jobs.delivery_diagnostics()
        state = next(item for item in diagnostics if item["job_id"] == pending["id"])
        assert state["delivery_state"] == "pending" and state["error"] == "broker_timeout"
        await container.jobs.cancel(owner, pending["id"])

        container.settings.job_owner_active_limit = 1
        first = await container.jobs.create(owner, "limit-one", {"source_job_id": str(uuid4())})
        try:
            await container.jobs.create(owner, "limit-two", {"source_job_id": str(uuid4())})
        except MapperError as error:
            assert error.status == 429 and error.retry_after > 0
        else:
            raise AssertionError("admission did not reject")
        await container.jobs.cancel(owner, first["id"])
        print(
            json.dumps(
                {
                    "postgresql_replicas": 10,
                    "unique_active_jobs": 1,
                    "unique_reservations": 1,
                    "real_matching": True,
                    "publish_timeout_injected": True,
                    "owner_admission_429": True,
                }
            ),
            flush=True,
        )
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(run())
