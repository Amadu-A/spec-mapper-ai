# src/worker.py
"""Bounded worker и outbox dispatcher с восстановлением lease, TTL и выключенным daily sync."""

from __future__ import annotations

import asyncio
import logging
from time import time
from uuid import UUID

from src.bootstrap import build_container
from src.config import Settings
from src.observability import configure_logging


async def maintenance(container) -> None:
    """Повторяет outbox после сбоев, очищает только задания и планирует разрешённую daily sync."""
    settings = container.settings
    last_cleanup = 0
    last_prune = 0
    while True:
        try:
            for job_id in await container.jobs.pending():
                await container.broker.publish(job_id)
                await container.jobs.dispatched(job_id)
            if time() - last_cleanup >= 30:
                await container.jobs.cleanup()
                last_cleanup = time()
                if settings.catalog_daily_sync:
                    status = await container.catalog.status()
                    if (
                        time() - (status.get("last_successful_sync") or 0)
                        >= settings.catalog_sync_seconds
                    ):
                        await container.jobs.create(
                            "system",
                            f"daily:{int(time() // settings.catalog_sync_seconds)}",
                            {},
                            "catalog",
                        )
            if time() - last_prune >= 3600:
                last_prune = time()
                await container.pruner.execute()
        except Exception:
            logging.getLogger(__name__).warning("maintenance_retry")
        await asyncio.sleep(3)


async def run() -> None:
    """Открывает только собственные клиентские соединения и обрабатывает ограниченное число jobs."""
    settings = Settings()
    settings.require_secrets()
    configure_logging(settings.log_level)
    container = build_container(settings)
    tasks = set()
    semaphore = asyncio.Semaphore(settings.worker_concurrency)

    def completed(task) -> None:
        """Забирает исключение task, не позволяя asyncio напечатать confidential URL драйвера."""
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).error("delivery_not_acknowledged")

    async def handle(message) -> None:
        """Подтверждает broker delivery после durable checkpoint; повтор защищён claim/fence."""
        try:
            async with message.process(requeue=True):
                try:
                    job_id = str(UUID(message.body.decode("ascii")))
                except (ValueError, UnicodeError):
                    logging.getLogger(__name__).warning("invalid_broker_message")
                    return
                await container.processor.process(job_id)
        finally:
            semaphore.release()

    dispatcher = asyncio.create_task(maintenance(container))
    try:
        while True:
            try:
                async for message in container.broker.messages():
                    await semaphore.acquire()
                    task = asyncio.create_task(handle(message))
                    tasks.add(task)
                    task.add_done_callback(completed)
            except Exception:
                logging.getLogger(__name__).warning("broker_reconnect")
                await asyncio.sleep(5)
    finally:
        dispatcher.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(dispatcher, *tasks, return_exceptions=True)
        await container.close()


if __name__ == "__main__":
    asyncio.run(run())
