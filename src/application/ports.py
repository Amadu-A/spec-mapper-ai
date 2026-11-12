# src/application/ports.py
"""Порты внешних систем; атомарные методы репозиториев образуют границу Unit of Work."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from src.domain.models import CatalogItem, ParsedCatalog


class JobRepository(Protocol):
    """Контракт долговечных заданий с изоляцией владельцев и fencing токенами."""

    async def create(self, owner: str, key: str, payload: dict, kind: str) -> dict:
        """Атомарно сохраняет задание и outbox либо возвращает идемпотентный ответ."""
        ...

    async def get(self, job_id: str, owner: str | None = None) -> dict:
        """Проверяет владельца и TTL до возврата снимка."""
        ...

    async def list(self, owner: str) -> list[dict]:
        """Возвращает только неистёкшие задания текущего владельца."""
        ...

    async def claim(self, job_id: str) -> dict | None:
        """Атомарно выдаёт lease; повторная доставка не начинает второй worker."""
        ...

    async def checkpoint(self, job_id: str, token: str, **changes) -> bool:
        """Применяет checkpoint только при действующем lease и совпадающем fence."""
        ...

    async def heartbeat(self, job_id: str, token: str) -> bool:
        """Продлевает действующий lease без права оживить истёкшее задание."""
        ...

    async def pending(self) -> list[str]:
        """Читает ограниченную пачку outbox и восстанавливает просроченные lease."""
        ...

    async def dispatched(self, job_id: str) -> None:
        """Отмечает подтверждённую брокером публикацию для ограниченного redelivery."""
        ...

    async def select(
        self, job_id: str, owner: str, index: int, item: dict | None, revision: int
    ) -> dict:
        """Атомарно сохраняет ручной выбор с optimistic concurrency и TTL."""
        ...

    async def cleanup(self) -> None:
        """Удаляет только временные задания и связанные исходные данные после TTL."""
        ...

    async def approve(
        self, job_id: str, owner: str, revision: int, key: str, snapshot: dict
    ) -> dict:
        """Атомарно фиксирует целое задание и единственный immutable snapshot без продления TTL."""
        ...

    async def approved(self, job_id: str, owner: str) -> dict:
        """Возвращает только утверждённый snapshot с проверкой owner и текущего TTL."""
        ...


class CatalogRepository(Protocol):
    """Снимки номенклатуры и постоянный кэш векторов, независимые от TTL заданий."""

    def lock(self) -> AbstractAsyncContextManager:
        """Исключает параллельную публикацию каталога на уровне PostgreSQL."""
        ...

    async def reconcile(self, source: ParsedCatalog) -> None:
        """Сохраняет стабильные внутренние UUID при появлении внешних GUID 1С."""
        ...

    def guard(self) -> AbstractAsyncContextManager:
        """Защищает pin, публикацию и сборку версий, не блокируя поиск во время embedding."""
        ...

    async def status(self) -> dict:
        """Возвращает активный снимок и последнюю диагностику синхронизации."""
        ...

    async def stage(self, version: str, items: list[CatalogItem], metadata: dict) -> None:
        """Записывает ещё не опубликованный снимок атомарно."""
        ...

    async def activate(self, version: str) -> None:
        """Меняет единственный активный указатель транзакцией, сохраняя предыдущий."""
        ...

    async def update_stage(self, version: str, metadata: dict) -> None:
        """Сохраняет проверенные финальные метаданные уже зарегистрированного staging снимка."""
        ...

    async def resumable(self, source_hash: str, identity: dict) -> dict | None:
        """Находит незавершённый staging того же XML и identity для восстановления checkpoints."""
        ...

    async def obsolete_versions(self) -> list[dict]:
        """Находит старые снимки, не используемые активным каталогом, rollback и живыми jobs."""
        ...

    async def discard_version(self, version: str) -> None:
        """Удаляет только подтверждённо неиспользуемый снимок под lock синхронизации."""
        ...

    async def sync_error(self, code: str) -> None:
        """Сохраняет диагностику последней неудачи без потери активного каталога."""
        ...

    async def items(self, version: str, guids: list[str]) -> dict[str, CatalogItem]:
        """Загружает все метаданные пачкой без N+1."""
        ...

    async def guids(self, version: str) -> list[str]:
        """Возвращает UUID снимка для вычисления удалённых позиций при incremental sync."""
        ...

    async def search(self, query: str, limit: int) -> list[dict]:
        """Ищет активные позиции по коду, имени и артикулу."""
        ...

    async def vectors(self, keys: list[str]) -> dict[str, list[float]]:
        """Возвращает ранее рассчитанные векторы по fingerprint и хешу позиции."""
        ...

    async def cache_vectors(self, vectors: dict[str, list[float]]) -> None:
        """Идемпотентно сохраняет векторы для инкрементальной синхронизации."""
        ...


class CatalogSource(Protocol):
    """Заменяемый источник снимка; следующий адаптер 1С использует тот же контракт."""

    async def read(self) -> ParsedCatalog:
        """Возвращает проверенные позиции и диагностику, не скрывая ошибки источника."""
        ...


class Embeddings(Protocol):
    """Контракт существующего shared-embedding без локального model runtime."""

    async def identity(self) -> dict:
        """Проверяет endpoint, identity и реальную размерность модели."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Векторизует ограниченную пачку с проверкой формата и конечности чисел."""
        ...


class VectorIndex(Protocol):
    """Стадийная индексация и поиск по конкретному неизменяемому снимку."""

    async def create(self, collection: str, dimension: int) -> None:
        """Создаёт новую коллекцию, не удаляя активную."""
        ...

    async def exists(self, collection: str) -> bool:
        """Определяет наличие зарегистрированного staging после сбоя создания/клонирования."""
        ...

    async def clone(self, source: str, target: str) -> None:
        """Копирует готовый snapshot с индексом вместо полной переиндексации неизменённых точек."""
        ...

    async def delete_points(self, collection: str, ids: list[str]) -> None:
        """Исключает удалённые и неактивные позиции только из staging снимка."""
        ...

    async def put(self, collection: str, points: list[dict]) -> None:
        """Идемпотентно записывает пачку UUID-точек."""
        ...

    async def count(self, collection: str) -> int:
        """Проверяет число точек перед публикацией."""
        ...

    async def remove(self, collection: str) -> None:
        """Идемпотентно удаляет только устаревшую project-specific коллекцию."""
        ...

    async def switch(self, collection: str) -> str | None:
        """Атомарно переключает alias, возвращая предыдущую коллекцию для rollback."""
        ...

    async def restore(self, collection: str | None) -> None:
        """Восстанавливает alias при неудачной фиксации указателя PostgreSQL."""
        ...

    async def search(
        self, collection: str, vectors: list[list[float]], limit: int
    ) -> list[list[tuple[str, float]]]:
        """Выполняет batched Top-K, сохраняя порядок входных строк."""
        ...


class ExtractionSource(Protocol):
    """Авторизованное получение только окончательного результата upstream."""

    async def fetch(self, source_job_id: str, progress) -> dict:
        """Ограниченно опрашивает статусы и однократно загружает готовый JSON."""
        ...


class TaskBroker(Protocol):
    """Долговечные сигналы заданий; PostgreSQL outbox устраняет потерю публикации."""

    async def publish(self, job_id: str) -> None:
        """Ждёт publisher confirm перед отметкой outbox."""
        ...

    def messages(self) -> AsyncIterator:
        """Выдаёт сообщения для обработки с явным подтверждением после checkpoint."""
        ...
