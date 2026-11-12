# src/application/event_ports.py
"""Узкие порты durable inbox 1С; application не зависит от SQL и HTTP."""

from typing import Protocol


class CatalogEventsRepository(Protocol):
    """Атомарные операции приёма, coalescing, диагностики и fencing событий."""

    async def receive(self, event: dict) -> tuple[dict, bool]:
        """Сохраняет inbox до 202 либо возвращает существующую доставку."""
        ...

    async def enqueue(self, config: dict) -> dict | None:
        """Атомарно связывает пачку с существующим durable job outbox."""
        ...

    async def load(self, job: dict) -> list[dict]:
        """Читает только привязанную пачку и проверяет действующий fence."""
        ...

    async def identities(self) -> dict:
        """Возвращает постоянные соответствия GUID и tombstones."""
        ...

    async def failure(self, job: dict, code: str) -> None:
        """Сохраняет безопасную диагностику неуспешной попытки текущего fence."""
        ...

    async def summary(self, journal: bool = False) -> dict:
        """Возвращает агрегаты; технический журнал добавляется только оператору."""
        ...
