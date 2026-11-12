# src/application/mappings.py
"""Пользовательские операции: идемпотентный запуск, ревью, поиск и неизменный оригинал."""

from __future__ import annotations

from uuid import UUID

from src.application.ports import CatalogRepository, JobRepository
from src.application.reports import export_json
from src.domain.models import (
    MapperError,
    confirm_rows,
    ensure_approvable,
    source_view,
    validate_source,
)
from src.observability import timed


class MappingUseCases:
    """Единые use-cases для API и web; владельца назначает только delivery слой."""

    def __init__(self, jobs: JobRepository, catalog: CatalogRepository, max_items: int) -> None:
        """Получает атомарные repository ports и лимит исходного документа."""
        self.jobs, self.catalog, self.max_items = jobs, catalog, max_items

    @timed("mapping_submit")
    async def submit(self, owner: str, key: str, source_job_id: str) -> dict:
        """Создаёт durable polling задание по UUID upstream без обращения к cookie
        другой системы.
        """
        try:
            source_job_id = str(UUID(source_job_id))
        except ValueError as exc:
            raise MapperError("invalid_job_id", "Укажите UUID задания извлечения") from exc
        return await self.jobs.create(owner, key, {"source_job_id": source_job_id}, "mapping")

    @timed("mapping_import_json")
    async def import_document(
        self, owner: str, key: str, document: dict, source_job_id: str | None = None
    ) -> dict:
        """Запускает тот же pipeline для независимого готового JSON, сохраняя исходные строки."""
        validate_source(document, self.max_items)
        payload = {"document": document}
        if source_job_id is not None:
            try:
                payload["source_job_id"] = str(UUID(source_job_id))
            except ValueError as exc:
                raise MapperError("invalid_job_id", "X-Source-Job-ID должен быть UUID") from exc
        return await self.jobs.create(owner, key, payload, "mapping")

    async def result(self, owner: str, job_id: str) -> dict:
        """Не выдаёт незавершённый подбор за готовый результат."""
        job = await self.jobs.get(job_id, owner)
        if job["kind"] != "mapping" or job["state"] not in {
            "complete",
            "ready_for_review",
            "approved",
        }:
            raise MapperError("result_not_ready", "Результат сопоставления пока не готов", 409)
        return job

    @timed("mapping_manual_selection")
    async def select(
        self, owner: str, job_id: str, index: int, guid: str | None, revision: int
    ) -> dict:
        """Разрешает выбор только существующей активной позиции; сохраняет отдельный snapshot."""
        if index < 0:
            raise MapperError("row_not_found", "Строка не найдена", 404)
        job = await self.result(owner, job_id)
        if job["state"] == "approved":
            raise MapperError("result_approved", "Утверждённый результат нельзя редактировать", 409)
        item = None
        if guid is not None:
            async with self.catalog.guard():
                status = await self.catalog.status()
                items = await self.catalog.items(status.get("version", ""), [guid])
                if guid not in items or not items[guid].active:
                    raise MapperError(
                        "catalog_item_unavailable",
                        "Позиция отсутствует в действующем каталоге",
                        409,
                    )
                item = {**items[guid].to_dict(), "catalog_version": status["version"]}
                return await self.jobs.select(job_id, owner, index, item, revision)
        return await self.jobs.select(job_id, owner, index, item, revision)

    @timed("mapping_approve")
    async def approve(self, owner: str, job_id: str, revision: int, key: str) -> dict:
        """Фиксирует общий результат; автоматические строки подтверждаются общей операцией."""
        job = await self.result(owner, job_id)
        if job["state"] != "approved":
            if job["revision"] != revision:
                raise MapperError(
                    "revision_conflict", "Результат уже изменён; обновите revision", 409
                )
            ensure_approvable(job["state"], job["data"]["rows"])
        snapshot_job = {**job, "data": {**job["data"], "rows": confirm_rows(job["data"]["rows"])}}
        return await self.jobs.approve(job_id, owner, revision, key, export_json(snapshot_job))


def public_status(job: dict) -> dict:
    """Убирает source, lease и owner из status; считает totals по всему заданию."""
    rows = job["data"].get("rows", [])
    counts = {
        name: sum(row["status"] == name for row in rows)
        for name in ("matched", "review", "not_found", "manual")
    }
    job_id = job["id"]
    return {
        "job_id": job_id,
        "kind": job["kind"],
        "status": job["state"],
        "stage": job["stage"],
        "source_job_id": job["data"].get("source_job_id"),
        "created_at": job["created_at"],
        "expires_at": job["expires_at"],
        "revision": job["revision"],
        "approved_at": job["data"].get("approval", {}).get("approved_at"),
        "approved_revision": job["data"].get("approval", {}).get("approved_revision"),
        "error_code": job["error_code"],
        "total": (
            len(job["data"]["source"]["items"])
            if job["data"].get("source")
            else job["data"].get("catalog_progress", {}).get("total", 0)
        ),
        "processed": (
            len(rows)
            if job["kind"] == "mapping"
            else job["data"].get("catalog_progress", {}).get("processed", 0)
        ),
        "unresolved_count": sum(
            row["status"] == "review" or row["status"] == "not_found" and not row.get("confirmed")
            for row in rows
        ),
        "counts": counts,
        "catalog_version": job["data"].get("catalog_version"),
        "links": {
            "status": f"/api/v1/mappings/{job_id}",
            "result": f"/api/v1/mappings/{job_id}/result",
            "xlsx": f"/api/v1/mappings/{job_id}/result.xlsx",
            "source": f"/api/v1/mappings/{job_id}/source.json",
            "approved": f"/api/v1/mappings/{job_id}/approved.json",
        },
    }


def result_page(job: dict, query: str, status: str, offset: int, limit: int) -> dict:
    """Фильтрует и страницы результата, сохраняя индексы и totals всего исходного документа."""
    data = job["data"]
    rows = [
        {"index": index, "source": source_view(source), **row}
        for index, (source, row) in enumerate(
            zip(data["source"]["items"], data["rows"], strict=True)
        )
        if (status == "all" or row["status"] == status)
        and query.casefold() in " ".join(source.values()).casefold()
    ]
    return {
        **public_status(job),
        "filtered_total": len(rows),
        "offset": offset,
        "items": rows[offset : offset + limit],
        "algorithm_version": data["algorithm_version"],
        "source_hash": data["source_hash"],
        "file_name": data["source"].get("file_name", ""),
    }
