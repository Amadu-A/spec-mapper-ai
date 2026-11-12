# src/infrastructure/catalog_events.py
"""Транзакционный inbox/outbox webhook на собственной PostgreSQL без новых shared ресурсов."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import time
from uuid import uuid4

from sqlalchemy import func, select, text

from src.domain.models import TERMINAL, MapperError, digest
from src.infrastructure.database import CatalogEventRow, CatalogIdentityRow, JobRow
from src.infrastructure.repositories import job_dict, new_job


def event_dict(row) -> dict:
    """Копирует технический durable event без карточек и credentials."""
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "event_id",
            "occurred_at",
            "received_at",
            "changes",
            "state",
            "attempts",
            "last_error",
            "applied_at",
            "catalog_version",
        )
    }


class SqlCatalogEvents:
    """Сериализует короткие inbox/coalescing транзакции между несколькими API/worker."""

    def __init__(self, sessions, settings, clock=time):
        """Получает pool и заменяемые часы для детерминированных recovery tests."""
        self.sessions, self.settings, self.clock = sessions, settings, clock
        self.test_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        """Использует общий management advisory lock; SQLite lock — только offline adapter."""
        async with self.test_lock:
            async with self.sessions.begin() as session:
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(7314086)"))
                yield session

    async def receive(self, event: dict) -> tuple[dict, bool]:
        """Отвечает только после commit; dedup происходит до проверки размера backlog."""
        now = self.clock()
        async with self.transaction() as session:
            row = await session.scalar(
                select(CatalogEventRow).where(
                    CatalogEventRow.source == "onec_catalog",
                    CatalogEventRow.event_id == event["event_id"],
                )
            )
            hashed = digest(event)
            if row:
                if row.payload_hash != hashed:
                    raise MapperError(
                        "event_id_conflict", "event_id уже принят с другим payload", 409
                    )
                return event_dict(row), True
            pending = await session.scalar(
                select(func.count())
                .select_from(CatalogEventRow)
                .where(CatalogEventRow.state != "applied")
            )
            if pending >= self.settings.onec_webhook_max_backlog:
                raise MapperError(
                    "event_backlog_full",
                    "Очередь событий заполнена; повторите доставку",
                    503,
                    retry_after=60,
                )
            row = CatalogEventRow(
                id=str(uuid4()),
                source="onec_catalog",
                event_id=event["event_id"],
                payload_hash=hashed,
                occurred_at=event["occurred_at"],
                received_at=now,
                changes=event["changes"],
                state="pending",
                attempts=0,
            )
            session.add(row)
            await session.flush()
            result = event_dict(row)
        return result, False

    async def enqueue(self, config: dict) -> dict | None:
        """Debounce и один batch job: crash до/после Rabbit confirm безопасен для inbox."""
        now = self.clock()
        async with self.transaction() as session:
            active = await session.scalar(
                select(JobRow.id)
                .where(
                    JobRow.kind.in_(["catalog", "catalog_events"]),
                    JobRow.state.not_in(TERMINAL),
                    JobRow.expires_at > now,
                )
                .limit(1)
            )
            if active:
                return None
            rows = list(
                await session.scalars(
                    select(CatalogEventRow)
                    .where(CatalogEventRow.state != "applied")
                    .order_by(CatalogEventRow.received_at)
                    .limit(self.settings.onec_webhook_max_events_per_batch)
                )
            )
            eligible = []
            for row in rows:
                if row.received_at + self.settings.onec_webhook_batch_window_seconds > now:
                    continue
                previous = await session.get(JobRow, row.job_id) if row.job_id else None
                if (
                    previous
                    and previous.state == "failed"
                    and previous.expires_at
                    - self.settings.retention_seconds
                    + min(300, 2 ** min(row.attempts, 8))
                    > now
                ):
                    continue
                eligible.append(row)
            if not eligible:
                return None
            job = new_job(
                "system:onec-events",
                str(uuid4()),
                {"catalog_sync": config, "catalog_events": [row.id for row in eligible]},
                "catalog_events",
                self.settings,
                now,
            )
            session.add(job)
            await session.flush()
            for row in eligible:
                row.job_id, row.state, row.attempts = job.id, "processing", row.attempts + 1
            result = job_dict(job)
        return result

    async def load(self, job: dict) -> list[dict]:
        """Проверяет привязку inbox, lease и состояние после crash между publish и checkpoint."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job["id"])
            if (
                not row
                or row.lease_token != job["lease_token"]
                or (row.lease_until or 0) <= self.clock()
            ):
                raise MapperError("lease_lost", "Пачка событий передана другому worker", 409)
            events = list(
                await session.scalars(
                    select(CatalogEventRow).where(
                        CatalogEventRow.id.in_(job["data"]["catalog_events"])
                    )
                )
            )
            if any(event.job_id != job["id"] for event in events) or len(events) != len(
                job["data"]["catalog_events"]
            ):
                raise MapperError("event_fence_lost", "Неверная привязка пачки событий", 409)
            return [event_dict(event) for event in events]

    async def identities(self) -> dict:
        """Читает только идентификаторы и tombstones, не дублируя каталог в inbox."""
        async with self.sessions() as session:
            return {
                row.onec_guid: {"guid": row.internal_guid, "tombstone_at": row.tombstone_at}
                for row in await session.scalars(select(CatalogIdentityRow))
            }

    async def failure(self, job: dict, code: str) -> None:
        """Старый fence не меняет applied и диагностику уже перепривязанной пачки."""
        async with self.sessions.begin() as session:
            current = await session.get(JobRow, job["id"])
            if not current or current.lease_token != job["lease_token"]:
                return
            for row in await session.scalars(
                select(CatalogEventRow).where(
                    CatalogEventRow.job_id == job["id"], CatalogEventRow.state != "applied"
                )
            ):
                row.last_error = code
                row.state = "failed"

    async def summary(self, journal=False) -> dict:
        """Обычная сессия получает только счётчики; журнал без GUID карточек доступен оператору."""
        async with self.sessions() as session:
            counts = dict(
                (
                    await session.execute(
                        select(CatalogEventRow.state, func.count()).group_by(CatalogEventRow.state)
                    )
                ).all()
            )
            result = {
                "received": sum(counts.values()),
                "pending": counts.get("pending", 0) + counts.get("processing", 0),
                "applied": counts.get("applied", 0),
                "failed": counts.get("failed", 0),
                "last_applied_at": await session.scalar(
                    select(func.max(CatalogEventRow.applied_at))
                ),
            }
            if journal:
                rows = await session.scalars(
                    select(CatalogEventRow).order_by(CatalogEventRow.received_at.desc()).limit(50)
                )
                result["items"] = [
                    {
                        k: v
                        for k, v in event_dict(row).items()
                        if k not in {"id", "changes", "occurred_at"}
                    }
                    | {"change_count": len(row.changes)}
                    for row in rows
                ]
            return result
