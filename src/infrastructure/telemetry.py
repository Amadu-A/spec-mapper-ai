# src/infrastructure/telemetry.py
"""Атомарные SQL-счётчики и технический аудит; не сохраняют документы после TTL."""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.domain.catalog_sync import METRIC_FIELDS, mapping_metrics, utc_day
from src.domain.models import TERMINAL, digest
from src.infrastructure.database import DailyMetricRow, SyncRunRow

REPORT_FIELDS = {
    "count",
    "version",
    "source_hash",
    "mode",
    "statistics",
    "embedded_count",
    "reused_count",
    "indexed_count",
    "removed_count",
    "added_count",
    "changed_count",
    "unchanged_count",
    "run_statistics",
    "error_code",
    "processed",
    "total",
    "catalog_version",
    "base_version",
    "downloaded_bytes",
}


def metric_delta(row, data: dict, state: str) -> tuple[dict, dict]:
    """Готовит delta до CAS записи job; SQL-счётчики меняются только после успешного CAS."""
    data = deepcopy(data)
    current = {**mapping_metrics(data, state), "jobs": 1}
    previous = row.data.get("_metrics", {})
    data["_metrics"] = current
    return data, {key: current[key] - previous.get(key, 0) for key in METRIC_FIELDS}


async def add_metrics(session, row, delta: dict) -> None:
    """Upsert счётчиков сериализуется PostgreSQL, включая конкурентное первое создание суток."""
    insert = sqlite_insert if session.bind.dialect.name == "sqlite" else pg_insert
    day = utc_day(row.created_at)
    for metric_day, scope in ((day, "all"), (day, digest(row.owner)), ("total", "all")):
        statement = insert(DailyMetricRow).values(day=metric_day, scope=scope, **delta)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[DailyMetricRow.day, DailyMetricRow.scope],
                set_={
                    key: getattr(DailyMetricRow, key) + getattr(statement.excluded, key)
                    for key in METRIC_FIELDS
                },
            )
        )


def safe_details(details: dict) -> dict:
    """Оставляет в долговечном отчёте только разрешённые технические поля и агрегаты."""
    return {key: deepcopy(value) for key, value in details.items() if key in REPORT_FIELDS}


async def record_catalog(session, row, changes: dict, now: float) -> None:
    """Обновляет историю вместе с fenced job checkpoint; события пишутся на смене этапа."""
    if row.kind != "catalog":
        return
    runs = list(await session.scalars(select(SyncRunRow).where(SyncRunRow.job_id == row.id)))
    if not runs:
        config = row.data.get("catalog_sync", {})
        run = SyncRunRow(
            id=row.id,
            job_id=row.id,
            owner=row.owner,
            source_type=config.get("source_type", "local_xml"),
            source_identity=digest(config or {"source_type": "local_xml"}),
            initiator="legacy_cli",
            created_at=row.created_at,
            details={},
            events=[],
        )
        session.add(run)
        runs = [run]
    state = changes.get("state", row.state)
    stage = changes.get("stage", row.stage)
    data = changes.get("data", row.data)
    details = safe_details(data.get("catalog_progress", {}))
    details.update(safe_details(data.get("catalog_status", {})))
    if "error_code" in changes:
        details["error_code"] = changes["error_code"]
    for run in runs:
        if not run.started_at and state != "queued":
            run.started_at = now
        if not run.events or run.events[-1]["stage"] != stage:
            run.events = [*run.events, {"at": now, "stage": stage, **safe_details(details)}]
        run.state, run.stage = state, stage
        run.details = {**(run.details or {}), **details}
        if state in TERMINAL:
            run.finished_at = now
