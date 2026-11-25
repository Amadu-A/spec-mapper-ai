# src/infrastructure/repositories.py
"""Транзакционные репозитории; только этот слой выполняет SQL и управляет фиксацией."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from time import time
from uuid import uuid4

from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from src.domain.embedding import compatible_embedding
from src.domain.invoices.adapter import invoice_items
from src.domain.lifecycle import (
    USER_KINDS,
    active_fingerprint,
    byte_size,
)
from src.domain.lifecycle import (
    snapshot as lifecycle_snapshot,
)
from src.domain.models import (
    TERMINAL,
    CatalogItem,
    MapperError,
    confirm_rows,
    digest,
    ensure_approvable,
)
from src.infrastructure.database import (
    ApprovalRow,
    CatalogEntryRow,
    CatalogEventRow,
    CatalogIdentityRow,
    CatalogStateRow,
    CatalogVersionRow,
    InvoiceUploadRow,
    JobEventRow,
    JobKeyRow,
    JobRow,
    SpecificationUploadRow,
    VectorCacheRow,
)
from src.infrastructure.telemetry import add_metrics, metric_delta, record_catalog


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
            "started_at",
            "updated_at",
            "last_event_id",
            "cancel_requested",
        )
    }


def new_job(owner: str, key: str, payload: dict, kind: str, settings, now: float) -> JobRow:
    """Создаёт одинаковый durable job для API, CLI и scheduler в транзакции вызывающего кода."""
    return JobRow(
        id=str(uuid4()),
        owner=owner,
        idempotency_hash=digest({"profile": kind, "key": key})
        if kind == "invoice"
        else digest(key),
        request_hash=digest({"kind": kind, "payload": payload}),
        kind=kind,
        created_at=now,
        expires_at=now
        + (
            settings.catalog_import_timeout_seconds
            if kind in {"catalog", "catalog_events"}
            else settings.draft_ttl_seconds
        ),
        updated_at=now,
        active_fingerprint=active_fingerprint(kind, payload) if kind in USER_KINDS else None,
        data={
            "source_job_id": payload.get("source_job_id"),
            "source": payload.get("document"),
            "rows": [],
            **({"specification": payload["specification"]} if "specification" in payload else {}),
            "ingestion": "import-json" if "document" in payload else "extracting",
            "upstream_mode": "import-json"
            if "document" in payload
            else (
                "managed"
                if payload.get("specification") or payload.get("invoice", {}).get("files")
                else "external"
            ),
            **({"invoice": payload["invoice"]} if "invoice" in payload else {}),
            **({"catalog_sync": payload["catalog_sync"]} if "catalog_sync" in payload else {}),
            **(
                {"catalog_events": payload["catalog_events"]} if "catalog_events" in payload else {}
            ),
        },
    )


class SqlJobs:
    """Хранит durable состояние, outbox, optimistic revision и fencing внутри транзакций."""

    def __init__(self, sessions, settings, clock=time) -> None:
        """Получает фабрику коротких транзакций и заменяемые часы для проверки TTL."""
        self.sessions, self.settings, self.clock = sessions, settings, clock
        self.test_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        """Сериализует только короткие SQL мутации своего агрегата; сеть не держит lock."""
        async with self.test_lock:
            async with self.sessions.begin() as session:
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(7314087)"))
                yield session

    async def event(self, session, row: JobRow, kind: str) -> None:
        """Пишет bounded safe metadata атомарно со статусом, без review revision и source."""
        if row.kind not in USER_KINDS:
            return
        value = lifecycle_snapshot(job_dict(row), self.clock())
        details = {
            key: value[key]
            for key in (
                "status",
                "stage",
                "processed_pages",
                "total_pages",
                "processed_items",
                "total_items",
                "current_file_index",
                "current_pdf_page",
                "error_code",
                "upstream_status",
            )
        }
        previous = await session.scalar(
            select(JobEventRow)
            .where(JobEventRow.job_id == row.id)
            .order_by(JobEventRow.id.desc())
            .limit(1)
        )
        if (
            previous
            and previous.kind == kind
            and previous.data == details
            and (row.lifecycle_metadata or {}).get("files") == value["files"]
            and (row.lifecycle_metadata or {}).get("revision") == row.revision
        ):
            return
        assert byte_size(details) <= 2048
        row.last_event_id = (row.last_event_id or 0) + 1
        row.updated_at = self.clock()
        row.lifecycle_metadata = {
            **value,
            "last_event_id": row.last_event_id,
            "updated_at": row.updated_at,
        }
        session.add(
            JobEventRow(
                job_id=row.id, id=row.last_event_id, at=row.updated_at, kind=kind, data=details
            )
        )
        await session.execute(
            delete(JobEventRow).where(
                JobEventRow.job_id == row.id,
                JobEventRow.id <= row.last_event_id - self.settings.lifecycle_event_limit,
            )
        )

    async def storage_guard(self, session, delta: int) -> None:
        """Не допускает неограниченного роста логических временных JSON/blobs/approval bytes."""
        if delta <= 0:
            return
        total = await session.scalar(
            select(func.coalesce(func.sum(JobRow.temporary_bytes), 0)).where(
                JobRow.expires_at > self.clock()
            )
        )
        if total + delta > self.settings.job_temporary_bytes_limit:
            raise MapperError(
                "temporary_storage_limit",
                "Временное хранилище занято",
                429,
                retry_after=self.settings.job_admission_retry_after,
            )

    async def admission(self, session, owner: str) -> None:
        """Проверяет глобальный и owner budget атомарно для пользовательских обработок."""
        filters = [
            JobRow.kind.in_(USER_KINDS),
            JobRow.state.not_in(TERMINAL),
            JobRow.expires_at > self.clock(),
        ]
        active = await session.scalar(select(func.count()).select_from(JobRow).where(*filters))
        own = await session.scalar(
            select(func.count()).select_from(JobRow).where(*filters, JobRow.owner == owner)
        )
        queued = await session.scalar(
            select(func.count()).select_from(JobRow).where(*filters, JobRow.state == "queued")
        )
        if (
            active >= self.settings.job_active_limit
            or own >= self.settings.job_owner_active_limit
            or queued >= self.settings.job_queued_limit
        ):
            raise MapperError(
                "queue_full",
                "Достигнут лимит активных заданий; повторите позже",
                429,
                retry_after=self.settings.job_admission_retry_after,
            )

    async def create(
        self,
        owner: str,
        key: str,
        payload: dict,
        kind: str = "mapping",
        *,
        pdf: bytes | None = None,
        uploads: list[bytes] | None = None,
    ) -> dict:
        """Атомарные idempotency aliases, active dedup, admission, blobs и outbox до 202."""
        key_hash = digest({"profile": kind, "key": key}) if kind == "invoice" else digest(key)
        request_hash = digest({"kind": kind, "payload": payload})
        for attempt in range(2):
            try:
                async with self.transaction() as session:
                    alias = await session.get(JobKeyRow, (owner, key_hash))
                    existing = (
                        await session.get(JobRow, alias.job_id)
                        if alias
                        else await session.scalar(
                            select(JobRow).where(
                                JobRow.owner == owner, JobRow.idempotency_hash == key_hash
                            )
                        )
                    )
                    if existing is not None:
                        self._check(existing, owner)
                        if (alias.request_hash if alias else existing.request_hash) != request_hash:
                            raise MapperError(
                                "idempotency_conflict",
                                "Ключ уже использован с другим запросом",
                                409,
                            )
                        return {**job_dict(existing), "reused": True}
                    fingerprint = active_fingerprint(kind, payload) if kind in USER_KINDS else None
                    if fingerprint:
                        await session.execute(
                            update(JobRow)
                            .where(
                                JobRow.active_fingerprint == fingerprint,
                                or_(JobRow.expires_at <= self.clock(), JobRow.state.in_(TERMINAL)),
                            )
                            .values(active_fingerprint=None)
                        )
                        candidates = await session.scalars(
                            select(JobRow).where(
                                JobRow.owner == owner,
                                JobRow.kind == kind,
                                JobRow.state.not_in(TERMINAL),
                                JobRow.expires_at > self.clock(),
                            )
                        )
                        for candidate in candidates:
                            data = candidate.data
                            original = (
                                {"document": data["source"]}
                                if data.get("ingestion") == "import-json"
                                else {"source_job_id": data.get("source_job_id")}
                            )
                            for field in ("invoice", "specification"):
                                if field in data:
                                    original[field] = data[field]
                                    if data.get("specification") or data.get("invoice", {}).get(
                                        "files"
                                    ):
                                        original.pop("source_job_id", None)
                            if data.get("ingestion") == "import-json" and data.get("source_job_id"):
                                original["source_job_id"] = data["source_job_id"]
                            matched = candidate.active_fingerprint or active_fingerprint(
                                kind, original
                            )
                            if matched == fingerprint:
                                session.add(
                                    JobKeyRow(
                                        owner=owner,
                                        key_hash=key_hash,
                                        request_hash=request_hash,
                                        job_id=candidate.id,
                                    )
                                )
                                await self.storage_guard(session, 512)
                                candidate.temporary_bytes += 512
                                return {**job_dict(candidate), "reused": True}
                        await self.admission(session, owner)
                    row = new_job(owner, key, payload, kind, self.settings, self.clock())
                    delta = None
                    if kind == "mapping":
                        row.data, delta = metric_delta(row, row.data, "queued")
                    row.temporary_bytes = (
                        byte_size(row.data)
                        + len(pdf or b"")
                        + sum(len(blob) for blob in uploads or [])
                        + self.settings.lifecycle_event_limit * 2048
                        + 512
                    )
                    await self.storage_guard(session, row.temporary_bytes)
                    session.add(row)
                    await session.flush()
                    session.add(
                        JobKeyRow(
                            owner=owner, key_hash=key_hash, request_hash=request_hash, job_id=row.id
                        )
                    )
                    if pdf is not None:
                        session.add(SpecificationUploadRow(job_id=row.id, content=pdf))
                    if uploads is not None:
                        session.add_all(
                            [
                                InvoiceUploadRow(job_id=row.id, file_index=index, content=body)
                                for index, body in enumerate(uploads)
                            ]
                        )
                    if delta is not None:
                        await add_metrics(session, row, delta)
                    else:
                        await record_catalog(
                            session, row, {"state": "queued", "stage": "queued"}, self.clock()
                        )
                    await self.event(session, row, "job_queued")
                    result = {**job_dict(row), "reused": False}
                return result
            except IntegrityError:
                if attempt:
                    raise
        raise AssertionError("Недостижимая ветвь")

    async def pdf(self, job_id: str) -> bytes:
        """Читает принятый PDF внутреннему worker до истечения TTL."""
        await self.get(job_id)
        async with self.sessions() as session:
            row = await session.get(SpecificationUploadRow, job_id)
            if row is None:
                raise MapperError("upload_missing", "Исходный PDF недоступен", 410)
            return row.content

    async def invoice_files(self, job_id: str) -> list[bytes]:
        """Читает blobs только внутреннему worker; чужие профили и истёкшие jobs запрещены."""
        job = await self.get(job_id)
        if job["kind"] != "invoice":
            raise MapperError("not_found", "Пакет не найден", 404)
        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(InvoiceUploadRow)
                    .where(InvoiceUploadRow.job_id == job_id)
                    .order_by(InvoiceUploadRow.file_index)
                )
            )
            if len(rows) != len(job["data"]["invoice"].get("files", [])):
                raise MapperError("upload_missing", "Исходный пакет недоступен", 410)
            return [row.content for row in rows]

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

    async def still_running(self, job_id: str, token: str) -> bool:
        """Проверяет отмену/fence перед внешним вызовом без чтения source/results."""
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    select(JobRow.id).where(
                        JobRow.id == job_id,
                        JobRow.lease_token == token,
                        JobRow.lease_until > self.clock(),
                        JobRow.expires_at > self.clock(),
                        JobRow.state.not_in(TERMINAL),
                        JobRow.cancel_requested.is_(False),
                    )
                )
            )

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

    async def active_catalog(self) -> dict | None:
        """CLI присоединяется к уже запущенному импорту, сохраняя его staging и outbox."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(JobRow)
                .where(
                    JobRow.kind == "catalog",
                    JobRow.state.not_in(TERMINAL),
                    JobRow.expires_at > self.clock(),
                )
                .order_by(JobRow.created_at)
                .limit(1)
            )
            return job_dict(row) if row else None

    async def claim(self, job_id: str, generation: int | None = None) -> dict | None:
        """Receipt и lease атомарны; ранний ACK дубля оставляет durable основание recovery."""
        now = self.clock()
        async with self.transaction() as session:
            row = await session.get(JobRow, job_id)
            if row is None:
                logging.getLogger(__name__).info(
                    "delivery_noop", extra={"job_id": job_id, "reason": "unknown_uuid"}
                )
                return None
            if row.state in TERMINAL or row.expires_at <= now:
                row.delivery_noops += 1
                logging.getLogger(__name__).info(
                    "delivery_noop", extra={"job_id": job_id, "reason": "terminal_or_expired"}
                )
                return None
            if generation is not None and generation != row.delivery_generation:
                row.delivery_noops += 1
                logging.getLogger(__name__).info(
                    "delivery_noop", extra={"job_id": job_id, "reason": "old_generation"}
                )
                return None
            row.delivery_state, row.delivery_received_at, row.delivery_until = "received", now, None
            if (row.lease_until or 0) > now:
                row.delivery_noops += 1
                logging.getLogger(__name__).info(
                    "delivery_noop", extra={"job_id": job_id, "reason": "lease_active"}
                )
                return None
            row.lease_token, row.lease_until = str(uuid4()), now + self.settings.lease_seconds
            row.attempts += 1
            row.started_at = row.started_at or now
            await self.event(session, row, "job_started")
            return job_dict(row)

    async def checkpoint(self, job_id: str, token: str, **changes) -> bool:
        """Общий fenced checkpoint, outbox retry и event фиксируются одной SQL транзакцией."""
        now = self.clock()
        retry_after = changes.pop("retry_after", 0)
        async with self.transaction() as session:
            row = await session.get(JobRow, job_id)
            if (
                row is None
                or row.lease_token != token
                or (row.lease_until or 0) <= now
                or row.expires_at <= now
                or row.state in TERMINAL
            ):
                return False
            target = changes.get("state", row.state)
            if row.cancel_requested and target not in {"cancelling", "cancelled"}:
                return False
            delta = None
            if row.kind == "mapping" and target in TERMINAL:
                changes["data"], delta = metric_delta(row, changes.get("data", row.data), target)
            if "data" in changes:
                growth = byte_size(changes["data"]) - byte_size(row.data)
                await self.storage_guard(session, growth)
                row.temporary_bytes += growth
            for name, value in changes.items():
                setattr(row, name, value)
            if target in TERMINAL:
                if target != "cancelled":
                    row.expires_at = now + self.settings.retention_seconds
                row.lease_until, row.lease_token = None, None
                row.active_fingerprint, row.delivery_state = None, "closed"
                row.delivery_until = None
            elif target in {"queued", "cancelling"}:
                row.lease_until, row.lease_token = None, None
                row.dispatched_at, row.delivery_token, row.delivery_until = None, None, None
                row.delivery_generation += 1
                row.delivery_state, row.delivery_next_at = "pending", now + retry_after
                row.delivery_published_at, row.delivery_received_at = None, None
            if delta is not None:
                await add_metrics(session, row, delta)
            await record_catalog(session, row, changes, now)
            await self.event(session, row, "job_" + target if target in TERMINAL else "progress")
            return True

    async def heartbeat(self, job_id: str, token: str) -> bool:
        """Истёкший fence и принятая отмена не продлевают работу прежнего исполнителя."""
        now = self.clock()
        async with self.transaction() as session:
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
        """Диагностический список pending: published больше не выбирается по таймеру 30 с."""
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(JobRow.id)
                    .where(
                        JobRow.state.not_in(TERMINAL),
                        JobRow.expires_at > self.clock(),
                        or_(
                            JobRow.delivery_state == "pending",
                            (JobRow.delivery_state == "received")
                            & (func.coalesce(JobRow.lease_until, 0) <= self.clock()),
                            (JobRow.delivery_state == "publishing")
                            & (func.coalesce(JobRow.delivery_until, 0) <= self.clock()),
                        ),
                    )
                    .order_by(JobRow.created_at)
                    .limit(100)
                )
            )

    async def reserve_delivery(self) -> dict | None:
        """Резервирует одну отправку без удержания транзакции во время AMQP."""
        from src.infrastructure.job_delivery import reserve

        return await reserve(self)

    async def confirm_delivery(self, attempt: dict) -> bool:
        """Применяет confirm только к зарезервированному поколению/токену."""
        from src.infrastructure.job_delivery import confirm

        return await confirm(self, attempt)

    async def delivery_failure(self, attempt: dict, code: str) -> bool:
        """Сохраняет bounded transport backoff независимо от attempts бизнес-обработки."""
        from src.infrastructure.job_delivery import failure

        return await failure(self, attempt, code)

    async def reconcile_delivery(self, identifiers: list[str]) -> dict:
        """Восстанавливает только явно указанные оператором потерянные UUID после drain."""
        from src.infrastructure.job_delivery import reconcile

        return await reconcile(self, identifiers)

    async def delivery_diagnostics(self) -> list[dict]:
        """Разделяет unique jobs, транспортные попытки и lease recovery без confidential data."""
        from src.infrastructure.job_delivery import diagnostics

        return await diagnostics(self)

    async def select(
        self, job_id: str, owner: str, index: int, item: dict | None, revision: int
    ) -> dict:
        """Сохраняет выбор одной строки целиком или выдаёт 409 при одновременном редактировании."""
        async with self.transaction() as session:
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
            data, delta = metric_delta(row, data, state) if row.kind == "mapping" else (data, None)
            growth = byte_size(data) - byte_size(row.data)
            await self.storage_guard(session, growth)
            row.temporary_bytes += growth
            changed = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.revision == revision,
                    JobRow.state.in_(("complete", "ready_for_review")),
                    JobRow.expires_at > self.clock(),
                )
                .values(data=data, revision=revision + 1, state=state)
            )
            if changed.rowcount != 1:
                raise MapperError("revision_conflict", "Результат изменён другим запросом", 409)
            if delta is not None:
                await add_metrics(session, row, delta)
            await self.event(session, row, "selection_changed")
        return await self.get(job_id, owner)

    async def metadata(self, session, row: JobRow) -> dict:
        """Читает safe cache; исторический fallback загружает source только одной записи."""
        value = deepcopy(row.lifecycle_metadata or {})
        if not value:
            info = {
                name: getattr(row, name)
                for name in (
                    "id",
                    "kind",
                    "state",
                    "stage",
                    "created_at",
                    "expires_at",
                    "revision",
                    "started_at",
                    "updated_at",
                    "last_event_id",
                    "cancel_requested",
                    "error_code",
                )
            }
            info["data"] = await session.scalar(select(JobRow.data).where(JobRow.id == row.id))
            value = lifecycle_snapshot(info, self.clock())
        value.update(
            expires_at=row.expires_at,
            revision=row.revision,
            last_event_id=row.last_event_id,
            cancel_requested=row.cancel_requested,
        )
        value["elapsed_seconds"] = max(0, self.clock() - (row.started_at or row.created_at))
        value["wait_seconds"] = max(0, (row.started_at or self.clock()) - row.created_at)
        return value

    async def lifecycle_view(self, owner: str, job_id: str) -> dict:
        """Owner/TTL до safe metadata; исторический fallback читает только одну запись."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(JobRow).options(defer(JobRow.data)).where(JobRow.id == job_id)
            )
            self._check(row, owner)
            if row.kind not in USER_KINDS:
                raise MapperError("not_found", "Задание не найдено", 404)
            value = await self.metadata(session, row)
            value["journal_start_id"] = await session.scalar(
                select(func.min(JobEventRow.id)).where(JobEventRow.job_id == job_id)
            )
            return value

    async def history_page(
        self, owner: str, profile: str, offset: int, limit: int, state: str = ""
    ) -> dict:
        """Общая пагинация своего профиля; review доступен для явного нового запуска."""
        filters = [
            JobRow.owner == owner,
            JobRow.expires_at > self.clock(),
            JobRow.kind == ("invoice" if profile == "invoice" else "mapping"),
        ]
        active_filters = [*filters, JobRow.state.not_in(TERMINAL)]
        if state == "active":
            filters.append(JobRow.state.not_in(TERMINAL))
        elif state:
            states = ["complete", "ready_for_review"] if state == "awaiting_review" else [state]
            filters.append(JobRow.state.in_(states))
        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(JobRow).where(*filters))
            rows = await session.scalars(
                select(JobRow)
                .options(defer(JobRow.data))
                .where(*filters)
                .order_by(JobRow.created_at.desc(), JobRow.id)
                .offset(offset)
                .limit(limit)
            )
            active_total = await session.scalar(
                select(func.count()).select_from(JobRow).where(*active_filters)
            )
            return {
                "total": total,
                "active_total": active_total,
                "offset": offset,
                "items": [await self.metadata(session, row) for row in rows],
            }

    async def lifecycle_events(self, owner: str, job_id: str, after: int) -> list[dict]:
        """Replay исключает cursor; утрата bounded журнала требует нового snapshot."""
        view = await self.lifecycle_view(owner, job_id)
        async with self.sessions() as session:
            first = await session.scalar(
                select(func.min(JobEventRow.id)).where(JobEventRow.job_id == job_id)
            )
            if after > view["last_event_id"] or first is not None and after < first - 1:
                raise MapperError(
                    "event_cursor_expired", "Обновите snapshot и подключитесь заново", 409
                )
            rows = await session.scalars(
                select(JobEventRow)
                .where(JobEventRow.job_id == job_id, JobEventRow.id > after)
                .order_by(JobEventRow.id)
                .limit(100)
            )
            return [
                {"id": row.id, "at": row.at, "kind": row.kind, "data": row.data} for row in rows
            ]

    async def cancel(self, owner: str, job_id: str) -> dict:
        """Сериализует cancel/approve/complete; обязанность отменить managed upstream durable."""
        async with self.transaction() as session:
            row = await session.get(JobRow, job_id)
            self._check(row, owner)
            if row.kind not in USER_KINDS:
                raise MapperError("not_found", "Задание не найдено", 404)
            if row.state in {"approved", "failed"}:
                raise MapperError(
                    "cancellation_not_allowed", "Терминальный результат изменить нельзя", 409
                )
            if row.cancel_requested:
                return job_dict(row)
            data = row.data
            managed = data.get("upstream_mode") == "managed" or bool(
                data.get("specification") or data.get("invoice", {}).get("files")
            )
            pending_upstream = (
                managed
                and data.get("source") is None
                and (
                    data.get("source_job_id")
                    or data.get("submission", {}).get("phase") == "sending"
                )
            )
            row.cancel_requested = True
            row.state = row.stage = "cancelling" if pending_upstream else "cancelled"
            row.error_code = None
            row.lease_until, row.lease_token = None, None
            row.delivery_generation += 1
            row.delivery_state = "pending" if pending_upstream else "closed"
            row.delivery_next_at = self.clock()
            row.delivery_token, row.delivery_until = None, None
            row.delivery_published_at, row.delivery_received_at = None, None
            if not pending_upstream:
                row.active_fingerprint = None
            await self.event(
                session, row, "cancellation_requested" if pending_upstream else "job_cancelled"
            )
            return job_dict(row)

    async def submission_accepted(self, job_id: str, operation: str, upstream_id: str) -> bool:
        """Сохраняет поздний ответ POST при cancel/fence только для связанной private операции."""
        async with self.transaction() as session:
            row = await session.get(JobRow, job_id)
            if (
                not row
                or row.expires_at <= self.clock()
                or row.data.get("submission", {}).get("token") != operation
            ):
                return False
            existing = row.data.get("source_job_id")
            if existing and existing != upstream_id:
                raise MapperError(
                    "upstream_id_conflict", "Ответ не совпадает с принятой операцией", 502
                )
            data = {
                **row.data,
                "source_job_id": upstream_id,
                "submission": {**row.data["submission"], "phase": "accepted"},
            }
            growth = byte_size(data) - byte_size(row.data)
            # ID принимается и при полном admission: обязанность cancel не теряется из-за quota.
            row.data, row.temporary_bytes = data, row.temporary_bytes + growth
            await self.event(session, row, "upstream_accepted")
            return True

    async def cleanup(self) -> None:
        """Удаляет временные mapping_jobs, сохраняя постоянный каталог и его векторы."""
        async with self.transaction() as session:
            for model in (JobKeyRow, JobEventRow):
                await session.execute(
                    delete(model).where(
                        model.job_id.in_(select(JobRow.id).where(JobRow.expires_at <= self.clock()))
                    )
                )
            await session.execute(
                delete(ApprovalRow).where(
                    ApprovalRow.job_id.in_(
                        select(JobRow.id).where(JobRow.expires_at <= self.clock())
                    )
                )
            )
            await session.execute(
                delete(SpecificationUploadRow).where(
                    SpecificationUploadRow.job_id.in_(
                        select(JobRow.id).where(JobRow.expires_at <= self.clock())
                    )
                )
            )
            await session.execute(
                delete(InvoiceUploadRow).where(
                    InvoiceUploadRow.job_id.in_(
                        select(JobRow.id).where(JobRow.expires_at <= self.clock())
                    )
                )
            )
            await session.execute(delete(JobRow).where(JobRow.expires_at <= self.clock()))

    async def approve(
        self, job_id: str, owner: str, revision: int, key: str, snapshot: dict
    ) -> dict:
        """Сериализует approve/selection и сохраняет неизменяемый снимок в одной транзакции."""
        async with self.transaction() as session:
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
                **(snapshot.get("approval", {}) if row.kind == "invoice" else {}),
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
            exported_items = (
                (item for _, _, item, _ in invoice_items(snapshot))
                if row.kind == "invoice"
                else snapshot["items"]
            )
            for item in exported_items:
                item["matching"]["approved"] = True
            data = {
                **deepcopy(row.data),
                "approval": approval,
                "rows": confirm_rows(row.data["rows"]),
            }
            data, delta = (
                metric_delta(row, data, "approved") if row.kind == "mapping" else (data, None)
            )
            growth = byte_size(data) - byte_size(row.data) + byte_size(snapshot)
            await self.storage_guard(session, growth)
            row.temporary_bytes += growth
            changed = await session.execute(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.revision == revision,
                    JobRow.state.in_(("complete", "ready_for_review")),
                    JobRow.expires_at > self.clock(),
                )
                .values(
                    data=data,
                    state="approved",
                    stage="approved",
                    delivery_state="closed",
                    active_fingerprint=None,
                )
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
            if delta is not None:
                await add_metrics(session, row, delta)
            await self.event(session, row, "job_approved")
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
                **(
                    {k: v for k, v in version.metadata_json.items() if not k.startswith("_")}
                    if version
                    else {}
                ),
            }

    async def reconcile(self, source) -> None:
        """Резервирует стабильные GUID под общим lock; immutable снимки и выборы не изменяет."""
        records = source.records or source.items
        if not any(item.onec_guid for item in records):
            return
        previous = await self.status()
        old = (
            await self.items(previous["version"], await self.guids(previous["version"]))
            if previous.get("ready")
            else {}
        )
        codes = {}
        for item in old.values():
            codes.setdefault(item.code, []).append(item)
        quarantined = set(previous.get("statistics", {}).get("conflicting_codes", []))
        current_codes = {}
        for item in records:
            current_codes[item.code] = current_codes.get(item.code, 0) + 1
        mapped = {}
        async with self.sessions.begin() as session:
            registry = {
                row.onec_guid: row for row in await session.scalars(select(CatalogIdentityRow))
            }
            used = {row.internal_guid for row in registry.values()}
            for item in records:
                if not item.onec_guid:
                    mapped[item.guid] = item
                    continue
                binding = registry.get(item.onec_guid)
                if binding is None:
                    legacy = codes.get(item.code, [])
                    safe = (
                        len(legacy) == 1
                        and not legacy[0].onec_guid
                        and item.code not in quarantined
                        and current_codes[item.code] == 1
                        and legacy[0].guid not in used
                    )
                    internal = legacy[0].guid if safe else item.onec_guid
                    binding = CatalogIdentityRow(
                        onec_guid=item.onec_guid, internal_guid=internal, code=item.code
                    )
                    session.add(binding)
                    registry[item.onec_guid] = binding
                    used.add(internal)
                binding.code = item.code
                mapped[item.guid] = replace(item, guid=binding.internal_guid)
        source.items = [mapped[item.guid] for item in source.items]
        source.records = [mapped[item.guid] for item in records]
        if source.context.get("event_context"):
            deleted = source.context["event_context"].get("tombstones", {})
            present = {item.onec_guid for item in source.records if item.onec_guid}
            counts = Counter(
                row.code
                for row in registry.values()
                if (not row.tombstone_at or row.onec_guid in present)
                and row.onec_guid not in deleted
            )
            counts.update(item.code for item in source.records if not item.onec_guid)
            conflicts = sorted(code for code, count in counts.items() if count > 1)
            if conflicts:
                source.items = [item for item in source.items if item.code not in conflicts]
                source.statistics["conflicting_codes"] = sorted(
                    set(source.statistics.get("conflicting_codes", [])) | set(conflicts)
                )
                source.statistics["active_count"] = len(source.items)
                source.statistics["quarantined_rows"] = sum(counts[code] for code in conflicts)
                source.warnings.append("Карантин GUID registry: " + ", ".join(conflicts))

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
                context = snapshot.metadata_json.get("_event_context", {})
                present = snapshot.metadata_json.get("_present_guids", [])
                if present or snapshot.metadata_json.get("_full_json"):
                    identities = await session.scalars(select(CatalogIdentityRow))
                    present_set = set(present)
                    for identity in identities:
                        if identity.onec_guid in present_set:
                            identity.tombstone_at = None
                        elif snapshot.metadata_json.get("_full_json"):
                            identity.tombstone_at = snapshot.metadata_json["_snapshot_at"]
                if snapshot.metadata_json.get("_full_json"):
                    await self._reconcile_events(session, snapshot.metadata_json, version)
                if context and not snapshot.metadata_json.get("published"):
                    job = await session.get(JobRow, context["job_id"], with_for_update=True)
                    if (
                        job is None
                        or job.lease_token != context["lease_token"]
                        or (job.lease_until or 0) <= time()
                    ):
                        raise MapperError("lease_lost", "Потеряна lease webhook job", 409)
                    for event in await session.scalars(
                        select(CatalogEventRow).where(CatalogEventRow.id.in_(context["event_ids"]))
                    ):
                        if event.job_id != job.id:
                            raise MapperError(
                                "event_fence_lost", "Пачка передана другому заданию", 409
                            )
                        event.state, event.applied_at, event.last_error, event.catalog_version = (
                            "applied",
                            time(),
                            None,
                            version,
                        )
                    for guid, occurred in context.get("tombstones", {}).items():
                        identity = await session.get(CatalogIdentityRow, guid)
                        if identity:
                            identity.tombstone_at = max(identity.tombstone_at or 0, occurred)
                        else:
                            session.add(
                                CatalogIdentityRow(
                                    onec_guid=guid,
                                    internal_guid=guid,
                                    code="",
                                    tombstone_at=occurred,
                                )
                            )
                snapshot.metadata_json = {**snapshot.metadata_json, "published": True}
            state.last_successful_sync, state.last_error = time(), None

    async def _reconcile_events(self, session, metadata: dict, version: str) -> None:
        """Полный подтверждённый snapshot сверяет только события, принятые до начала download."""
        at, present = metadata["_snapshot_at"], set(metadata["_present_guids"])
        rows = await session.scalars(
            select(CatalogEventRow).where(
                CatalogEventRow.state != "applied",
                CatalogEventRow.received_at <= at,
                CatalogEventRow.occurred_at <= at,
            )
        )
        for event in rows:
            for change in event.changes:
                if change["guid"] not in present:
                    binding = await session.get(CatalogIdentityRow, change["guid"])
                    if binding:
                        binding.tombstone_at = at
                    else:
                        session.add(
                            CatalogIdentityRow(
                                onec_guid=change["guid"],
                                internal_guid=change["guid"],
                                code="",
                                tombstone_at=at,
                            )
                        )
                        await session.flush()
            event.state, event.applied_at, event.last_error, event.catalog_version = (
                "applied",
                time(),
                None,
                version,
            )

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
