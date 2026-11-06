# src/infrastructure/broker.py
"""Выделенная durable очередь общего RabbitMQ с publisher confirms и ограниченным prefetch."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aio_pika


class RabbitBroker:
    """Сообщение содержит только UUID задания; confidential payload остаётся в PostgreSQL."""

    def __init__(self, settings) -> None:
        """Сохраняет настройки project user/vhost без открытия соединения в конструкторе."""
        self.settings, self.connection, self.channel, self.queue = settings, None, None, None

    async def connect(self) -> None:
        """Восстанавливает соединение и объявляет только собственную durable очередь."""
        if self.connection is not None and not self.connection.is_closed:
            return
        self.connection = await aio_pika.connect_robust(self.settings.amqp_url, timeout=10)
        self.channel = await self.connection.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=self.settings.worker_concurrency)
        self.queue = await self.channel.declare_queue(self.settings.rabbitmq_queue, durable=True)

    async def publish(self, job_id: str) -> None:
        """Ждёт broker confirm; сбой оставляет outbox для повторной публикации."""
        await self.connect()
        await self.channel.default_exchange.publish(
            aio_pika.Message(
                body=job_id.encode("ascii"),
                message_id=job_id,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=self.settings.rabbitmq_queue,
            mandatory=True,
            timeout=10,
        )

    async def messages(self) -> AsyncIterator:
        """Возвращает сообщения с ручным ack, оставляя redelivery под контролем lease."""
        await self.connect()
        async with self.queue.iterator() as messages:
            async for message in messages:
                yield message

    async def close(self) -> None:
        """Завершает только проектное клиентское соединение, не управляя shared broker."""
        if self.connection is not None:
            await self.connection.close()

    async def ready(self) -> None:
        """Проверяет реального consumer; остановленный worker делает API not ready."""
        await self.connect()
        result = await self.queue.declare()
        if result.consumer_count < 1:
            raise ValueError("Worker не подключён к очереди")
