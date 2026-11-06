# src/infrastructure/sync_repository.py
"""SQL UoW настроек, UTC scheduler и аудита поверх существующих durable catalog jobs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.domain.catalog_sync import (
    METRIC_FIELDS,
    due_occurrence,
    next_occurrence,
    sync_defaults,
    utc_day,
)
from src.domain.models import TERMINAL, MapperError, digest
from src.infrastructure.database import (
    CatalogAuditRow,
    DailyMetricRow,
    JobRow,
    OperatorTicketRow,
    ServiceActivityRow,
    SyncRunRow,
    SyncSettingsRow,
)
from src.infrastructure.repositories import job_dict, new_job
from src.infrastructure.telemetry import add_metrics, metric_delta, record_catalog, safe_details


def run_dict(row: SyncRunRow) -> dict:
    """Отвязывает технический отчёт от ORM без owner и открытых настроек подключения."""
    return {
        key: deepcopy(getattr(row, key))
        for key in (
            "id",
            "job_id",
            "source_type",
            "initiator",
            "created_at",
            "started_at",
            "finished_at",
            "state",
            "stage",
            "details",
            "events",
        )
    }


class SqlSyncRepository:
    """Сериализует trigger/settings/schedule в PostgreSQL; SQLite используется только offline."""

    def __init__(self, sessions, settings, jobs) -> None:
        """Получает существующие session factory/clock, не создавая отдельного процесса
        scheduler.
        """
        self.sessions, self.settings_model, self.jobs = sessions, settings, jobs
        self.local_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        """Общая короткая блокировка для двух worker replicas и одновременного manual trigger."""
        async with self.local_lock:
            async with self.sessions.begin() as session:
                if session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(7314086)"))
                yield session

    async def _settings(self, session) -> SyncSettingsRow:
        """Bootstrap выполняется один раз; последующие env изменения не перезаписывают GUI."""
        now = self.jobs.clock()
        row = await session.get(SyncSettingsRow, 1)
        if row is None:
            row = SyncSettingsRow(id=1, config={}, initialized=False, probe_result={})
            session.add(row)
        if not row.initialized:
            row.config = sync_defaults(self.settings_model)
            row.initialized, row.revision = True, 0
            row.updated_at, row.updated_by, row.metrics_started_at = now, "bootstrap", now
            row.next_run_at = (
                next_occurrence(now, row.config["time_utc"]) if row.config["enabled"] else None
            )
            row.probe_result = {}
            existing = await session.scalars(
                select(JobRow).where(JobRow.kind == "mapping", JobRow.expires_at > now)
            )
            for job in existing:
                if "_metrics" not in job.data:
                    data, delta = metric_delta(job, job.data, job.state)
                    job.data = data
                    await add_metrics(session, job, delta)
            await session.flush()
        return row

    def _public_settings(self, row: SyncSettingsRow) -> dict:
        """Готовит внутренний DTO application; HTTP-use-case отдельно исключает private поля."""
        return {
            **deepcopy(row.config),
            "revision": row.revision,
            "updated_at": row.updated_at,
            "next_run_at": row.next_run_at,
            "probe": deepcopy(row.probe_result or {}),
        }

    async def settings(self) -> dict:
        """Читает постоянную конфигурацию независимо от TTL импортов и browser session."""
        async with self.transaction() as session:
            return self._public_settings(await self._settings(session))

    async def save(self, config: dict, revision: int, actor: str) -> dict:
        """CAS сохраняет настройки без enqueue; изменение времени не зависит от last_success."""
        async with self.transaction() as session:
            row = await self._settings(session)
            if row.revision != revision:
                raise MapperError(
                    "revision_conflict", "Настройки изменены; перечитайте revision", 409
                )
            previous = row.config
            unchanged_schedule = all(
                previous.get(k) == config.get(k) for k in ("enabled", "time_utc", "source_type")
            )
            row.config, row.revision = deepcopy(config), revision + 1
            row.updated_at, row.updated_by = self.jobs.clock(), actor
            row.next_run_at = (
                (
                    row.next_run_at
                    if unchanged_schedule and row.next_run_at is not None
                    else next_occurrence(self.jobs.clock(), config["time_utc"])
                )
                if config["enabled"]
                else None
            )
            if row.probe_result.get("source_identity") != config.get("_source_identity"):
                row.probe_result = {}
            elif row.probe_result:
                row.probe_result = {**row.probe_result, "revision": row.revision}
            self._audit(session, actor, "settings_saved", {"revision": row.revision})
            return self._public_settings(row)

    def _audit(self, session, actor: str, event: str, details: dict) -> None:
        """Пишет только заранее выбранные технические значения без URL/credential."""
        session.add(
            CatalogAuditRow(
                id=str(uuid4()), at=self.jobs.clock(), actor=actor, event=event, details=details
            )
        )

    async def _active(self, session) -> JobRow | None:
        """Учитывает и старые catalog jobs, начатые CLI до появления новой миграции."""
        return await session.scalar(
            select(JobRow)
            .where(
                JobRow.kind == "catalog",
                JobRow.expires_at > self.jobs.clock(),
                JobRow.state.not_in(TERMINAL),
            )
            .order_by(JobRow.created_at)
            .limit(1)
        )

    async def _create(
        self,
        session,
        owner: str,
        key: str,
        config: dict,
        initiator: str,
        schedule_key: str | None = None,
        coalesced: JobRow | None = None,
    ) -> dict:
        """Job/outbox и run записываются одной транзакцией; coalesced не создаёт второй job."""
        payload = {"catalog_sync": config}
        if coalesced is None:
            row = new_job(owner, key, payload, "catalog", self.settings_model, self.jobs.clock())
            session.add(row)
            await session.flush()
        else:
            row = coalesced
        run = SyncRunRow(
            id=row.id if coalesced is None else str(uuid4()),
            job_id=row.id,
            owner=owner,
            source_type=config.get("source_type", "local_xml"),
            source_identity=config.get("_source_identity", digest(config)),
            initiator=initiator,
            schedule_key=schedule_key,
            created_at=self.jobs.clock(),
            state=row.state,
            stage=row.stage,
            details={},
            events=[{"at": self.jobs.clock(), "stage": "coalesced" if coalesced else "queued"}],
        )
        session.add(run)
        return job_dict(row)

    async def trigger(self, owner: str, key: str, config: dict, initiator: str = "manual") -> dict:
        """Повтор одного ключа возвращает job; другой ключ во время импорта даёт 409."""
        async with self.transaction() as session:
            await self._settings(session)
            existing = await session.scalar(
                select(JobRow).where(
                    JobRow.owner == owner,
                    JobRow.idempotency_hash == digest(key),
                )
            )
            if existing:
                self.jobs._check(existing, owner)
                expected = digest({"kind": "catalog", "payload": {"catalog_sync": config}})
                legacy = config["source_type"] == "local_xml" and existing.request_hash == digest(
                    {"kind": "catalog", "payload": {}}
                )
                if existing.request_hash != expected and not legacy:
                    raise MapperError(
                        "idempotency_conflict", "Ключ использован с другим источником", 409
                    )
                return job_dict(existing)
            active = await self._active(session)
            if active:
                raise MapperError(
                    "catalog_busy", "Импорт уже идёт; дождитесь завершения текущего job", 409
                )
            return await self._create(session, owner, key, config, initiator)

    async def schedule(self, source_identity: str, revision: int | None = None) -> dict | None:
        """Захватывает последний UTC-слот ровно один раз и фиксирует следующий до
        публикации outbox.
        """
        async with self.transaction() as session:
            row = await self._settings(session)
            if revision is not None and row.revision != revision:
                return None
            now = self.jobs.clock()
            if not row.config["enabled"] or row.next_run_at is None or row.next_run_at > now:
                return None
            slot = due_occurrence(now, row.config["time_utc"])
            key = utc_day(slot) + ":" + source_identity
            row.next_run_at = next_occurrence(now, row.config["time_utc"])
            existing = await session.scalar(
                select(SyncRunRow).where(SyncRunRow.schedule_key == key)
            )
            if existing:
                return None
            config = {**row.config, "_source_identity": source_identity}
            active = await self._active(session)
            if active:
                active_config = active.data.get("catalog_sync", {"source_type": "local_xml"})
                if (
                    active_config.get("source_type") != config["source_type"]
                    or active_config.get("_source_identity", source_identity) != source_identity
                ):
                    self._audit(
                        session,
                        "scheduler",
                        "schedule_skipped_busy",
                        {"scheduled_date": utc_day(slot)},
                    )
                    return None
            initiator = "scheduled_catch_up" if now - slot > 60 else "scheduled"
            if active:
                initiator += "_coalesced"
            return await self._create(
                session, "system", "schedule:" + key, config, initiator, key, active
            )

    async def probe(self, revision: int, result: dict) -> dict:
        """Сохраняет результат probe только для той же конфигурации, которую проверяли."""
        async with self.transaction() as session:
            row = await self._settings(session)
            if row.revision != revision:
                raise MapperError(
                    "revision_conflict", "Конфигурация изменилась во время проверки", 409
                )
            row.probe_result = {key: value for key, value in result.items() if key != "_actor"}
            row.probe_result.update(revision=revision, checked_at=self.jobs.clock())
            self._audit(
                session,
                result.get("_actor", "operator"),
                "connection_test",
                {"state": result["state"], "revision": revision},
            )
            return self._public_settings(row)

    async def event(self, job_id: str, stage: str, details: dict) -> None:
        """Агрегирует source progress; основной state machine по-прежнему принадлежит
        JobProcessor.
        """
        async with self.sessions.begin() as session:
            row = await session.get(JobRow, job_id)
            if row:
                await record_catalog(
                    session,
                    row,
                    {
                        "stage": stage,
                        "state": stage,
                        "data": {**row.data, "catalog_progress": safe_details(details)},
                    },
                    self.jobs.clock(),
                )

    async def history(
        self, offset: int = 0, limit: int = 20, state: str = "", source: str = ""
    ) -> dict:
        """Пагинирует долговечные запуски; пустая история не подменяется текущим count каталога."""
        filters = []
        if state:
            filters.append(SyncRunRow.state == state)
        if source:
            filters.append(SyncRunRow.source_type == source)
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(SyncRunRow).where(*filters)
            )
            rows = await session.scalars(
                select(SyncRunRow)
                .where(*filters)
                .order_by(SyncRunRow.created_at.desc(), SyncRunRow.id)
                .offset(offset)
                .limit(limit)
            )
            return {"total": total, "offset": offset, "items": [run_dict(row) for row in rows]}

    async def report(self, run_id: str) -> dict:
        """Даёт агрегаты и события после истечения документа, не восстанавливая XML или
        source JSON.
        """
        async with self.sessions() as session:
            row = await session.get(SyncRunRow, run_id)
            if not row:
                raise MapperError("not_found", "Отчёт импорта не найден", 404)
            return run_dict(row)

    async def summary(self, owner: str | None) -> dict:
        """Знаменатель KPI — позиции завершённых jobs, созданных в выбранные UTC-сутки."""
        async with self.transaction() as session:
            settings = await self._settings(session)
            scope = "all" if owner is None else digest(owner)
            rows = list(
                await session.scalars(select(DailyMetricRow).where(DailyMetricRow.scope == scope))
            )
            today = utc_day(self.jobs.clock())
            daily = next((row for row in rows if row.day == today), None)
            values = {key: getattr(daily, key) if daily else 0 for key in METRIC_FIELDS}
            current_day = datetime.fromtimestamp(self.jobs.clock(), UTC).date()
            delta = None
            if settings.metrics_started_at <= self.jobs.clock() - 14 * 86400:
                start = (current_day - timedelta(days=14)).isoformat()
                middle = (current_day - timedelta(days=7)).isoformat()
                old = [row for row in rows if start <= row.day < middle]
                recent = [row for row in rows if middle <= row.day < today]
                old_n, recent_n = sum(r.positions for r in old), sum(r.positions for r in recent)
                if old_n and recent_n:
                    delta = 100 * (
                        sum(r.matched for r in recent) / recent_n
                        - sum(r.matched for r in old) / old_n
                    )
            return {
                "scope": "global" if owner is None else "own",
                "day_utc": today,
                **values,
                "jobs_total": (
                    next((row.jobs for row in rows if row.day == "total"), 0)
                    if owner is None
                    else sum(row.jobs for row in rows)
                ),
                "automatic_percent": 100 * values["matched"] / values["positions"]
                if values["positions"]
                else None,
                "review_percent": 100 * values["unresolved"] / values["positions"]
                if values["positions"]
                else None,
                "weekly_delta_pp": delta,
                "coverage_started_at": settings.metrics_started_at,
                "metrics_retention_days": self.settings_model.dashboard_metrics_retention_days,
            }

    async def jobs_page(
        self,
        owner: str | None,
        offset: int = 0,
        limit: int = 10,
        state: str = "",
        order: str = "desc",
    ) -> dict:
        """Оператор видит метаданные; чужие source/result/selection endpoints остаются закрытыми."""
        from src.application.mappings import public_status

        filters = [JobRow.kind == "mapping", JobRow.expires_at > self.jobs.clock()]
        if owner is not None:
            filters.append(JobRow.owner == owner)
        if state:
            filters.append(JobRow.state == state)
        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(JobRow).where(*filters))
            sort = JobRow.created_at.asc() if order == "asc" else JobRow.created_at.desc()
            rows = await session.scalars(
                select(JobRow).where(*filters).order_by(sort, JobRow.id).offset(offset).limit(limit)
            )
            items = []
            for row in rows:
                own = owner == row.owner
                integration = (
                    row.owner[4:] if row.owner in {"api:onec", "api:telegram"} else "legacy"
                )
                source = (
                    "Web"
                    if row.owner.startswith("web:")
                    else (
                        "spec-extracting-ai"
                        if row.data.get("ingestion") == "extracting"
                        else "import-json (" + integration + ")"
                    )
                )
                items.append(
                    {**public_status(job_dict(row)), "source_type": source, "can_open": own}
                )
            return {"total": total, "offset": offset, "items": items}

    async def touch(self, name: str) -> None:
        """Фиксирует успешный технический сигнал без key value и identity документов."""
        async with self.sessions.begin() as session:
            insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
            statement = insert(ServiceActivityRow).values(name=name, last_seen=self.jobs.clock())
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ServiceActivityRow.name],
                    set_={"last_seen": statement.excluded.last_seen},
                )
            )

    async def activity(self, name: str) -> float | None:
        """Возвращает последнее настоящее обращение; отсутствие значения означает
        отсутствие сигнала.
        """
        async with self.sessions() as session:
            return await session.scalar(
                select(ServiceActivityRow.last_seen).where(ServiceActivityRow.name == name)
            )

    async def audit(self, offset: int = 0, limit: int = 20, event: str = "") -> dict:
        """Возвращает отдельную страницу административных событий без конфигурационных значений."""
        async with self.sessions() as session:
            filters = [CatalogAuditRow.event == event] if event else []
            total = await session.scalar(
                select(func.count()).select_from(CatalogAuditRow).where(*filters)
            )
            rows = await session.scalars(
                select(CatalogAuditRow)
                .where(*filters)
                .order_by(CatalogAuditRow.at.desc())
                .offset(offset)
                .limit(limit)
            )
            return {
                "total": total,
                "offset": offset,
                "items": [
                    {"id": r.id, "at": r.at, "event": r.event, "details": r.details} for r in rows
                ],
            }

    async def cleanup(self) -> None:
        """Чистит только собственные технические таблицы, сохраняя активные run и дневные KPI."""
        now = self.jobs.clock()
        async with self.sessions.begin() as session:
            cutoff = now - self.settings_model.catalog_audit_retention_days * 86400
            orphaned = await session.scalars(
                select(SyncRunRow).where(
                    SyncRunRow.state.not_in(TERMINAL),
                    SyncRunRow.job_id.not_in(select(JobRow.id).where(JobRow.expires_at > now)),
                )
            )
            for row in orphaned:
                row.state, row.stage, row.finished_at = "failed", "failed", now
                row.details = {**row.details, "error_code": "catalog_import_expired"}
                row.events = [
                    *row.events,
                    {"at": now, "stage": "failed", "error_code": "catalog_import_expired"},
                ]
            await session.execute(
                delete(SyncRunRow).where(
                    SyncRunRow.created_at < cutoff, SyncRunRow.state.in_(TERMINAL)
                )
            )
            await session.execute(delete(CatalogAuditRow).where(CatalogAuditRow.at < cutoff))
            await session.execute(
                delete(DailyMetricRow).where(
                    DailyMetricRow.day
                    < utc_day(now - self.settings_model.dashboard_metrics_retention_days * 86400)
                )
            )
            await session.execute(
                delete(OperatorTicketRow).where(OperatorTicketRow.expires_at < now)
            )
            await session.execute(
                delete(ServiceActivityRow).where(
                    ServiceActivityRow.name.startswith("operator:"),
                    ServiceActivityRow.last_seen
                    < now - self.settings_model.operator_session_seconds,
                )
            )
