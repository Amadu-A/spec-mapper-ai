# src/domain/catalog_sync.py
"""Календарное UTC-расписание и безопасные агрегаты без документов и внешнего I/O."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from src.domain.models import MapperError

METRIC_FIELDS = ("jobs", "positions", "matched", "manual", "review", "unresolved", "failed")


def utc_day(timestamp: float) -> str:
    """Возвращает календарные UTC-сутки независимо от timezone процесса/браузера."""
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def validate_time(value: str) -> str:
    """Отклоняет неоднозначные/локальные часы; принимается только HH:MM UTC."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise MapperError("invalid_schedule_time", "Укажите время UTC в формате HH:MM", 422)
    return value


def next_occurrence(now: float, time_utc: str) -> float:
    """Выбирает следующий календарный слот, не сдвигая время из-за длительности импорта."""
    validate_time(time_utc)
    hour, minute = map(int, time_utc.split(":"))
    current = datetime.fromtimestamp(now, UTC)
    slot = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot.timestamp() <= now:
        slot += timedelta(days=1)
    return slot.timestamp()


def due_occurrence(now: float, time_utc: str) -> float:
    """Выбирает только последний пропущенный слот: downtime не создаёт очередь за все сутки."""
    return next_occurrence(now, time_utc) - 86400


def mapping_metrics(data: dict, state: str) -> dict[str, int]:
    """Считает завершённые позиции; manual не входит в автоматические matched."""
    result = dict.fromkeys(METRIC_FIELDS, 0)
    if state == "failed":
        result["failed"] = 1
    if state not in {"complete", "ready_for_review", "approved"}:
        return result
    rows = data.get("rows", [])
    result["positions"] = len(rows)
    for row in rows:
        if row["status"] in {"matched", "manual", "review"}:
            result[row["status"]] += 1
        result["unresolved"] += row["status"] == "review" or (
            row["status"] == "not_found" and not row.get("confirmed")
        )
    return result


def sync_defaults(settings) -> dict:
    """Готовит открытые bootstrap-параметры; после инициализации применяется запись PostgreSQL."""
    return {
        "enabled": settings.catalog_daily_sync,
        "time_utc": settings.catalog_sync_time_utc,
        "source_type": "local_xml",
        "url": settings.onec_catalog_url,
        "method": settings.onec_catalog_http_method,
        "auth_type": settings.onec_catalog_auth_type,
        "credential_ref": "onec_catalog",
        "expected_format": settings.onec_catalog_response_format,
        "export_mode": settings.onec_catalog_export_mode,
        "timeout_seconds": settings.onec_catalog_timeout_seconds,
        "max_bytes": settings.max_xml_bytes,
        "completion_header": settings.onec_catalog_completion_header,
        "completion_value": settings.onec_catalog_completion_value,
    }
