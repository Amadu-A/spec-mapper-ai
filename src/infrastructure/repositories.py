# src/infrastructure/repositories.py
"""Транзакционные репозитории; только этот слой выполняет SQL и управляет фиксацией."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from time import time
from uuid import uuid4

from sqlalchemy import delete, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from src.domain.embedding import compatible_embedding
from src.domain.models import TERMINAL, CatalogItem, MapperError, digest, ensure_approvable
from src.infrastructure.database import (
    ApprovalRow,
    CatalogEntryRow,
    CatalogStateRow,
    CatalogVersionRow,
    JobRow,
    VectorCacheRow,
)


def job_dict(row: JobRow) -> dict:
    """Создаёт отвязанный снимок агрегата без ORM-объектов за границей data access."""
    return {
        name: deepcopy(getattr(row, name))
        for name in (
            "id",
            "owner",
            "kind",
            "state",
            "stage",
            "created_at",
            "expires_at",
            "lease_token",
            "attempts",
            "revision",
            "error_code",
            "data",
        )
    }


class SqlJobs:
    """Хранит durable состояние, outbox, optimistic revision и fencing внутри транзакций."""

    def __init__(self, sessions, settings, clock=time) -> None:
        """Получает фабрику коротких транзакций и заменяемые часы для проверки TTL."""
        self.sessions, self.settings, self.clock = sessions, settings, clock

    async def create(self, owner: str, key: str, payload: dict, kind: str = "mapping") -> dict:
        """Уникальный owner/key защищает и от последовательных, и от конкурентных дублей."""
        key_hash, request_hash = digest(key), digest({"kind": kind, "payload": payload})
        try:
            async with self.sessions.begin() as session:
                row = JobRow(
                    id=str(uuid4()),
                    owner=owner,
                    idempotency_hash=key_hash,
                    request_hash=request_hash,
                    kind=kind,
                    created_at=self.clock(),
                    expires_at=self.clock()
                    + (
                        self.settings.catalog_import_timeout_seconds
                        if kind == "catalog"
                        else self.settings.draft_ttl_seconds
                    ),
                    data={
                        "source_job_id": payload.get("source_job_id"),
                        "source": payload.get("document"),
                        "rows": [],
                    },
                )
                session.add(row)
                await session.flush()
                result = job_dict(row)
            return result
        except IntegrityError:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(JobRow).where(JobRow.owner == owner, JobRow.idempotency_hash == key_hash)
                )
                if row is None:
                    raise
                self._check(row, owner)
                if row.request_hash != request_hash:
                    raise MapperError(
                        "idempotency_conflict", "Ключ уже использован с другим запросом", 409
                    ) from None
                return job_dict(row)

    def _check(self, row: JobRow | None, owner: str | None) -> None:
        """Сначала скрывает чужие задания, затем запрещает доступ после TTL до
        физической очистки.
        """
        if row is None or owner is not None and row.owner != owner:
            raise MapperError("not_found", "Задание не найдено", 404)
        if row.expires_at <= self.clock():
            raise MapperError("expired", "Срок хранения задания истёк", 410)

    async def get(self, job_id: str, owner: str | None = None) -> dict:
        """Возвращает только допустимый по владельцу и времени снимок."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job_id)
            self._check(row, owner)
            return job_dict(row)

    async def list(self, owner: str) -> list[dict]:
        """Ограничивает историю последними 50 неистёкшими заданиями одной сессии."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.owner == owner, JobRow.expires_at > self.clock())
                .order_by(JobRow.created_at.desc())
                .limit(50)
            )
            return [job_dict(row) for row in rows]

    async def claim(self, job_id: str) -> dict | None:
        """CAS-обновление выдаёт единственный lease даже при нескольких доставках RabbitMQ."""
        now = self.clock()
        async with self.sessions.begin() as session:
            row = await session.scalar(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.expires_at > now,
                    JobRow.state.not_in(TERMINAL),
                    or_(JobRow.lease_until.is_(None), JobRow.lease_until <= now),
                )
                .values(
                    lease_token=str(uuid4()),
                    lease_until=now + self.settings.lease_seconds,
                    attempts=JobRow.attempts + 1,
                )
                .returning(JobRow)
            )
            return job_dict(row) if row else None

    async def checkpoint(self, job_id: str, token: str, **changes) -> bool:
        """Fenced checkpoint не позволяет старому worker перезаписать результат нового."""
        now = self.clock()
        if changes.get("state") in TERMINAL:
            changes.update(
                expires_at=now + self.settings.retention_seconds, lease_until=None, lease_token=None
            )
        elif changes.get("state") == "queued":
            changes.update(lease_until=None, lease_token=None, dispatched_at=None)
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.lease_token == token,
                    JobRow.lease_until > now,
                    JobRow.expires_at > now,
                    JobRow.state.not_in(TERMINAL),
                )
                .values(**changes)
            )
            return result.rowcount == 1

    async def heartbeat(self, job_id: str, token: str) -> bool:
        """Продлевает только ещё действующий lease, не изменяя часовой TTL черновика."""
        now = self.clock()
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.lease_token == token,
                    JobRow.lease_until > now,
                    JobRow.expires_at > now,
                    JobRow.state.not_in(TERMINAL),
                )
                .values(lease_until=now + self.settings.lease_seconds)
            )
            return result.rowcount == 1

    async def pending(self) -> list[str]:
        """Повторяет неподтверждённый outbox и восстанавливает задания после смерти worker."""
        now = self.clock()
        async with self.sessions() as session:
            ids = await session.scalars(
                select(JobRow.id)
                .where(
                    JobRow.state.not_in(TERMINAL),
                    JobRow.expires_at > now,
                    or_(JobRow.lease_until.is_(None), JobRow.lease_until <= now),
                    or_(JobRow.dispatched_at.is_(None), JobRow.dispatched_at < now - 30),
                )
                .order_by(JobRow.created_at)
                .limit(100)
            )
            return list(ids)

    async def dispatched(self, job_id: str) -> None:
        """Фиксирует publisher confirm; повтор после сбоя всё равно безопасен через claim."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(JobRow).where(JobRow.id == job_id).values(dispatched_at=self.clock())
            )

    async def select(
        self, job_id: str, owner: str, index: int, item: dict | None, revision: int
    ) -> dict:
        """Сохраняет выбор одной строки целиком или выдаёт 409 при одновременном редактировании."""
        async with self.sessions.begin() as session:
            row = await session.scalar(select(JobRow).where(JobRow.id == job_id).with_for_update())
            self._check(row, owner)
            if row.state not in {"complete", "ready_for_review"} or index >= len(row.data["rows"]):
                raise MapperError("invalid_state", "Строка недоступна для ручного выбора", 409)
            if row.revision != revision:
                raise MapperError(
                    "revision_conflict", "Результат уже изменён; обновите таблицу", 409
                )
            data = deepcopy(row.data)
            data["rows"][index].update(
                selection=item,
                status="manual" if item else "not_found",
                confirmed=True,
                manually_reviewed=True,
            )
            state = (
                "ready_for_review"
                if any(
                    r["status"] == "review" or not r["confirmed"] and r["status"] == "not_found"
                    for r in data["rows"]
                )
                else "complete"
            )
            changed = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.revision == revision,
                    JobRow.expires_at > self.clock(),
                )
                .values(data=data, revision=revision + 1, state=state)
            )
            if changed.rowcount != 1:
                raise MapperError("revision_conflict", "Результат изменён другим запросом", 409)
        return await self.get(job_id, owner)

    async def cleanup(self) -> None:
        """Удаляет временные mapping_jobs, сохраняя постоянный каталог и его векторы."""
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ApprovalRow).where(
                    ApprovalRow.job_id.in_(
                        select(JobRow.id).where(JobRow.expires_at <= self.clock())
                    )
                )
            )
            await session.execute(delete(JobRow).where(JobRow.expires_at <= self.clock()))

    async def approve(
        self, job_id: str, owner: str, revision: int, key: str, snapshot: dict
    ) -> dict:
        """Сериализует approve/selection и сохраняет неизменяемый снимок в одной транзакции."""
        async with self.sessions.begin() as session:
            row = await session.scalar(select(JobRow).where(JobRow.id == job_id).with_for_update())
            self._check(row, owner)
            existing = await session.get(ApprovalRow, job_id)
            if existing is not None:
                if existing.revision != revision:
                    raise MapperError("revision_conflict", "Утверждена другая revision", 409)
                return job_dict(row)
            if row.revision != revision:
                raise MapperError(
                    "revision_conflict", "Результат уже изменён; обновите revision", 409
                )
            ensure_approvable(row.state, row.data["rows"])
            approved_at = self.clock()
            approval = {
                "approved_at": approved_at,
                "approved_revision": revision,
                "owner": owner,
                "catalog_version": row.data["catalog_version"],
            }
            snapshot = {
                **deepcopy(snapshot),
                "status": "approved",
                "approval": approval,
                "approved_at": approved_at,
                "approved_revision": revision,
            }
            for item in snapshot["items"]:
                item["matching"]["approved"] = True
            data = {**deepcopy(row.data), "approval": approval}
            changed = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.revision == revision,
                    JobRow.state.in_(("complete", "ready_for_review")),
                    JobRow.expires_at > self.clock(),
                )
                .values(data=data, state="approved", stage="approved")
            )
            if changed.rowcount != 1:
                raise MapperError("revision_conflict", "Результат изменён другим запросом", 409)
            session.add(
                ApprovalRow(
                    job_id=job_id,
                    owner=owner,
                    revision=revision,
                    approved_at=approved_at,
                    idempotency_hash=digest(key),
                    snapshot=snapshot,
                )
            )
        return await self.get(job_id, owner)

    async def approved(self, job_id: str, owner: str) -> dict:
        """Выдаёт неизменяемый финальный JSON после проверки владельца и срока."""
        await self.get(job_id, owner)
        async with self.sessions() as session:
            approved = await session.get(ApprovalRow, job_id)
            if approved is None or approved.owner != owner:
                raise MapperError("not_approved", "Результат ещё не утверждён", 409)
            return deepcopy(approved.snapshot)


class SqlCatalog:
    """Версионированный PostgreSQL-каталог с advisory lock и кэшем векторов."""

    def __init__(self, sessions, engine) -> None:
        """Принимает общий pool; локальный lock используется только в SQLite-тестах."""
        self.sessions, self.engine, self.test_lock = sessions, engine, asyncio.Lock()
        self.snapshot_lock = asyncio.Lock()

    @asynccontextmanager
    async def guard(self):
        """Сериализует короткие операции pin/publish/prune отдельно от долгого импорта."""
        if self.engine.dialect.name == "sqlite":
            async with self.snapshot_lock:
                yield
            return
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT pg_advisory_lock(7314085)"))
            try:
                yield
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(7314085)"))

    @asynccontextmanager
    async def lock(self):
        """Сериализует импорты между процессами; connection loss освобождает PostgreSQL lock."""
        if self.engine.dialect.name == "sqlite":
            async with self.test_lock:
                yield
            return
        async with self.engine.connect() as connection:
            acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(7314084)"))
            if not acquired:
                raise MapperError("catalog_busy", "Синхронизация каталога уже выполняется", 409)
            try:
                yield
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(7314084)"))

    async def status(self) -> dict:
        """Читает активную версию независимо от незаконченного staging импорта."""
        async with self.sessions() as session:
            state = await session.get(CatalogStateRow, 1)
            version = (
                await session.get(CatalogVersionRow, state.active)
                if state and state.active
                else None
            )
            return {
                "ready": version is not None,
                "version": state.active if state else None,
                "last_successful_sync": state.last_successful_sync if state else None,
                "last_error": state.last_error if state else None,
                **(version.metadata_json if version else {}),
            }

    async def stage(self, version: str, items: list[CatalogItem], metadata: dict) -> None:
        """Фиксирует полный снимок в одной транзакции; активный указатель пока не меняется."""
        async with self.sessions.begin() as session:
            session.add(CatalogVersionRow(id=version, metadata_json=metadata, created_at=time()))
            for offset in range(0, len(items), 500):
                await session.execute(
                    insert(CatalogEntryRow),
                    [
                        {
                            "version": version,
                            "guid": item.guid,
                            "code": item.code,
                            "name": item.name,
                            "article": item.article,
                            "item_data": item.to_dict(),
                        }
                        for item in items[offset : offset + 500]
                    ],
                )

    async def activate(self, version: str) -> None:
        """Публикует указатель и время успешной синхронизации одной транзакцией."""
        async with self.sessions.begin() as session:
            state = await session.get(CatalogStateRow, 1)
            if not state:
                state = CatalogStateRow(id=1)
                session.add(state)
            if state.active != version:
                state.previous, state.active = state.active, version
            snapshot = await session.get(CatalogVersionRow, version)
            if snapshot is not None:
                snapshot.metadata_json = {**snapshot.metadata_json, "published": True}
            state.last_successful_sync, state.last_error = time(), None

    async def resumable(self, source_hash: str, identity: dict) -> dict | None:
        """Возвращает только непубликовавшийся снимок с совместимыми XML и embedding identity."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CatalogVersionRow)
                .where(
                    CatalogVersionRow.metadata_json["source_hash"].as_string() == source_hash,
                )
                .order_by(CatalogVersionRow.created_at.desc())
            )
            for row in rows:
                if (
                    not row.metadata_json.get("published")
                    and "resume_offset" in row.metadata_json
                    and compatible_embedding(
                        row.metadata_json.get(
                            "embedding_identity", {"fingerprint": row.metadata_json["fingerprint"]}
                        ),
                        identity,
                    )
                ):
                    return {"version": row.id, **deepcopy(row.metadata_json)}
        return None

    async def update_stage(self, version: str, metadata: dict) -> None:
        """Фиксирует окончательные counts и fingerprint до публикации активного указателя."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(CatalogVersionRow)
                .where(CatalogVersionRow.id == version)
                .values(metadata_json=metadata)
            )

    async def obsolete_versions(self) -> list[dict]:
        """Сохраняет active/previous и снимки всех неистёкших заданий, включая failed staging."""
        async with self.sessions() as session:
            state = await session.get(CatalogStateRow, 1)
            protected = {state.active, state.previous} if state else set()
            referenced = await session.scalars(
                select(JobRow.data["catalog_version"].as_string()).where(JobRow.expires_at > time())
            )
            protected.update(referenced)
            bases = await session.scalars(
                select(JobRow.data["catalog_progress"]["base_version"].as_string()).where(
                    JobRow.expires_at > time()
                )
            )
            protected.update(bases)
            rows = await session.scalars(
                select(CatalogVersionRow).where(CatalogVersionRow.created_at < time() - 3600)
            )
            return [
                {"version": row.id, **row.metadata_json}
                for row in rows
                if row.id not in protected
                and not (
                    not row.metadata_json.get("published")
                    and row.metadata_json.get("updated_at", 0) > time() - 86400
                )
            ]

    async def discard_version(self, version: str) -> None:
        """После повторной проверки удаляет metadata и позиции только одного obsolete снимка."""
        if version not in {row["version"] for row in await self.obsolete_versions()}:
            raise MapperError("catalog_version_in_use", "Снимок каталога ещё используется", 409)
        async with self.sessions.begin() as session:
            await session.execute(delete(CatalogEntryRow).where(CatalogEntryRow.version == version))
            await session.execute(delete(CatalogVersionRow).where(CatalogVersionRow.id == version))

    async def sync_error(self, code: str) -> None:
        """Сохраняет код последней неудачи, не подменяя рабочий снимок пустым."""
        async with self.sessions.begin() as session:
            state = await session.get(CatalogStateRow, 1)
            if not state:
                state = CatalogStateRow(id=1)
                session.add(state)
            state.last_error = code

    async def items(self, version: str, guids: list[str]) -> dict[str, CatalogItem]:
        """Читает metadata пачками с учётом лимита bind parameters SQLite и PostgreSQL."""
        result = {}
        async with self.sessions() as session:
            ids = list(set(guids))
            for offset in range(0, len(ids), 500):
                rows = await session.scalars(
                    select(CatalogEntryRow).where(
                        CatalogEntryRow.version == version,
                        CatalogEntryRow.guid.in_(ids[offset : offset + 500]),
                    )
                )
                result.update({row.guid: CatalogItem(**row.item_data) for row in rows})
        return result

    async def search(self, query: str, limit: int) -> list[dict]:
        """Экранирует LIKE и ищет по разрешённому активному снимку без N+1."""
        status = await self.status()
        if not status["ready"]:
            return []
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CatalogEntryRow)
                .where(
                    CatalogEntryRow.version == status["version"],
                    or_(
                        CatalogEntryRow.code.ilike(pattern, escape="\\"),
                        CatalogEntryRow.name.ilike(pattern, escape="\\"),
                        CatalogEntryRow.article.ilike(pattern, escape="\\"),
                    ),
                )
                .order_by(CatalogEntryRow.code, CatalogEntryRow.guid)
                .limit(limit)
            )
            return [row.item_data for row in rows]

    async def guids(self, version: str) -> list[str]:
        """Читает только UUID предыдущего снимка без загрузки больших metadata строк."""
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(CatalogEntryRow.guid).where(CatalogEntryRow.version == version)
                )
            )

    async def vectors(self, keys: list[str]) -> dict[str, list[float]]:
        """Получает embedding-кэш одной пачкой для текущего ограниченного batch."""
        async with self.sessions() as session:
            rows = await session.scalars(select(VectorCacheRow).where(VectorCacheRow.key.in_(keys)))
            return {row.key: row.vector for row in rows}

    async def cache_vectors(self, vectors: dict[str, list[float]]) -> None:
        """Записывает новые векторы под общим lock синхронизации каталога."""
        if vectors:
            async with self.sessions.begin() as session:
                await session.execute(
                    insert(VectorCacheRow), [{"key": k, "vector": v} for k, v in vectors.items()]
                )
