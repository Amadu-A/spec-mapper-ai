# src/application/invoices/service.py
"""Два входа invoice-профиля и финансовый guard поверх общих CAS/approval use-cases."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter

from src.application.invoices.ports import InvoiceExtraction, Upload, UploadInspector
from src.application.mappings import MappingUseCases
from src.application.ports import JobRepository
from src.domain.invoices.adapter import invoice_items, matchable
from src.domain.invoices.models import validate_invoice
from src.domain.invoices.warnings import approval_context, financial_warnings, selection_warnings
from src.domain.models import MapperError
from src.observability import timed


def selected_pages(value: str, page_count: int) -> list[int]:
    """Проверяет физические номера до построения ranges; порядок страниц канонический."""
    if not value.strip():
        return list(range(1, page_count + 1))
    if len(value) > 2000:
        raise MapperError("invalid_pages", "Слишком длинный выбор страниц", 422)
    pages = set()
    for part in value.split(","):
        if not re.fullmatch(r"\s*\d+(?:\s*-\s*\d+)?\s*", part):
            raise MapperError("invalid_pages", "Укажите страницы, например 1-3,5", 422)
        bounds = [int(number) for number in part.split("-")]
        first, last = bounds[0], bounds[-1]
        if not 1 <= first <= last <= page_count:
            raise MapperError("invalid_pages", "Страница вне исходного файла", 422)
        pages.update(range(first, last + 1))
    return sorted(pages)


class Invoices:
    """Один процесс для uploads/import-json; подбор, выбор и фиксация не дублируются."""

    def __init__(
        self,
        jobs: JobRepository,
        mappings: MappingUseCases,
        extraction: InvoiceExtraction,
        inspector: UploadInspector,
        settings,
    ) -> None:
        """Получает общий storage/engine и профильные входные адаптеры из bootstrap."""
        self.jobs, self.mappings, self.extraction = jobs, mappings, extraction
        self.inspector, self.settings = inspector, settings

    @timed("invoice_submit")
    async def submit(self, owner: str, key: str, uploads: list[Upload], selection: dict) -> dict:
        """Сохраняет все ограниченные blobs и outbox атомарно перед 202, без ожидания VLM."""
        settings = self.settings
        if not 1 <= len(uploads) <= settings.invoice_max_files:
            raise MapperError("invoice_file_limit", "Допустимо от 1 до 10 файлов", 413)
        if (
            not isinstance(selection, dict)
            or set(selection) - {str(i) for i in range(len(uploads))}
            or any(not isinstance(value, str) for value in selection.values())
        ):
            raise MapperError("invalid_pages", "Некорректный словарь выбора страниц", 422)
        files, contents, normalized = [], [], {}
        total_bytes, total_pages = 0, 0
        for index, upload in enumerate(uploads):
            content = bytearray()
            async for chunk in upload.chunks:
                total_bytes += len(chunk)
                if (
                    len(content) + len(chunk) > settings.invoice_max_file_bytes
                    or total_bytes > settings.invoice_max_batch_bytes
                ):
                    raise MapperError(
                        "upload_limit", "Превышен размер файла или пакета счетов", 413
                    )
                content.extend(chunk)
            if not content:
                raise MapperError("invalid_invoice_file", "Исходный файл пуст", 422)
            filename = re.split(r"[/\\]", upload.filename)[-1]
            if (
                not filename
                or len(filename) > 180
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)
            ):
                raise MapperError("invalid_filename", "Некорректное имя исходного файла", 422)
            body = bytes(content)
            media_type, count = await asyncio.to_thread(
                self.inspector.inspect, body, upload.media_type
            )
            chosen = selected_pages(selection.get(str(index), ""), count)
            total_pages += len(chosen)
            if total_pages > settings.invoice_max_selected_pages:
                raise MapperError("page_limit", "Превышен суммарный лимит выбранных страниц", 413)
            files.append(
                {
                    "file_index": index,
                    "filename": filename,
                    "media_type": media_type,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "page_count": count,
                    "selected_pages": chosen,
                    "size_bytes": len(body),
                }
            )
            normalized[str(index)] = ",".join(map(str, chosen))
            contents.append(body)
        return await self.jobs.create(
            owner,
            key,
            {
                "invoice": {
                    "files": files,
                    "page_selection": normalized,
                }
            },
            "invoice",
            uploads=contents,
        )

    @timed("invoice_import_json")
    async def import_document(self, owner: str, key: str, document: dict) -> dict:
        """Принимает тот же конечный source без VLM/нормализации, сохраняя полный payload hash."""
        validate_invoice(document, self.settings.invoice_max_items)
        return await self.jobs.create(owner, key, {"document": document, "invoice": {}}, "invoice")

    async def get(self, owner: str, job_id: str) -> dict:
        """Применяет owner/TTL до профильной проверки: чужое 404, своё истёкшее 410."""
        job = await self.jobs.get(job_id, owner)
        if job["kind"] != "invoice":
            raise MapperError("not_found", "Счёт не найден", 404)
        return job

    async def rows(
        self, owner: str, job_id: str, query: str, status: str, offset: int, limit: int
    ) -> dict:
        """Возвращает сохранённые строки с исходными реквизитами и стабильной адресацией."""
        job = await self.mappings.result(owner, job_id)
        rows = []
        for index, ((file, document, source, reference), row) in enumerate(
            zip(invoice_items(job["data"]["source"]), job["data"]["rows"], strict=True)
        ):
            if status != "all" and row["status"] != status:
                continue
            text = " ".join(
                str(source.get(field) or "")
                for field in (
                    "caption",
                    "brand",
                    "manufacturer",
                    "article",
                    "article_candidate",
                    "supplier_code",
                )
            )
            if query.casefold() not in text.casefold():
                continue
            rows.append(
                {
                    "index": index,
                    "source": source,
                    "selectable": matchable(source),
                    "reference": reference,
                    **row,
                    "warnings": selection_warnings(row),
                    "file": {k: v for k, v in file.items() if k != "documents"},
                    "document": {k: v for k, v in document.items() if k != "items"},
                }
            )
        return {
            **invoice_status(job),
            "items": rows[offset : offset + limit],
            "offset": offset,
            "filtered_total": len(rows),
            "source_hash": job["data"]["source_hash"],
            "algorithm_version": job["data"]["algorithm_version"],
            "financial_warnings": financial_warnings(job["data"]["source"]),
        }

    async def select(
        self, owner: str, job_id: str, index: int, guid: str | None, revision: int
    ) -> dict:
        """Передаёт товарный выбор общему use-case; платёж не становится номенклатурой."""
        job = await self.mappings.result(owner, job_id)
        entries = list(invoice_items(job["data"]["source"]))
        if not 0 <= index < len(entries):
            raise MapperError("row_not_found", "Строка не найдена", 404)
        if not matchable(entries[index][2]):
            raise MapperError(
                "row_not_matchable", "Эта строка требует отдельного решения в 1С", 409
            )
        return await self.mappings.select(owner, job_id, index, guid, revision)

    @timed("invoice_approve")
    async def approve(
        self, owner: str, job_id: str, revision: int, key: str, acknowledged: list[str]
    ) -> dict:
        """Фиксирует финансовое согласие вместе с общим approval/CAS, не исправляя суммы."""
        job = await self.mappings.result(owner, job_id)
        if job["state"] != "approved" and job["revision"] != revision:
            raise MapperError("revision_conflict", "Результат изменён; перечитайте revision", 409)
        context = (
            approval_context(job["data"]["source"], acknowledged)
            if job["state"] != "approved"
            else None
        )
        return await self.mappings.approve(owner, job_id, revision, key, approval_context=context)

    async def preview(
        self, owner: str, job_id: str, file_index: int, page: int, width: int
    ) -> bytes:
        """Проверяет локальные координаты до запроса защищённого preview 8083."""
        job = await self.get(owner, job_id)
        files = job["data"]["invoice"].get("files", [])
        if not files or not job["data"].get("source_job_id"):
            raise MapperError("preview_unavailable", "Для этого JSON исходный файл недоступен", 409)
        if not 0 <= file_index < len(files) or not 1 <= page <= files[file_index]["page_count"]:
            raise MapperError("not_found", "Страница не найдена", 404)
        return await self.extraction.preview(job["data"]["source_job_id"], file_index, page, width)


def invoice_status(job: dict) -> dict:
    """Публичный статус без upstream ID/адресов, исходных реквизитов и секретов."""
    data, state = job["data"], job["state"]
    if state in {"complete", "ready_for_review"}:
        state = "awaiting_review"
    elif state not in {"queued", "approved", "failed"}:
        state = "matching" if data.get("source") else "extracting"
    source, rows = data.get("source"), data["rows"]
    root = f"/api/v1/invoices/{job['id']}"
    summary = source["summary"] if source else {}
    return {
        "job_id": job["id"],
        "status": state,
        "revision": job["revision"],
        "created_at": job["created_at"],
        "expires_at": job["expires_at"],
        "approved_at": data.get("approval", {}).get("approved_at"),
        "error_code": job["error_code"],
        "file_count": summary.get("file_count", len(data["invoice"].get("files", []))),
        "document_count": summary.get("document_count", 0),
        "total": summary.get("item_count", 0),
        "processed": len(rows),
        "counts": dict(Counter(row["status"] for row in rows)),
        "unresolved_count": sum(
            row["status"] == "review" or row["status"] == "not_found" and not row["confirmed"]
            for row in rows
        ),
        "financial_warning_count": len(financial_warnings(source)) if source else 0,
        "upstream_warning": data.get("upstream_status") == "completed_with_warnings",
        "preview_available": bool(data["invoice"].get("files") and data.get("source_job_id")),
        "catalog_version": data.get("catalog_version"),
        "links": {
            "status": root,
            "rows": root + "/rows",
            "candidates": root + "/candidates",
            "source": root + "/source.json",
            "draft": root + "/draft.json",
            "result": root + "/result.json",
            "xlsx": root + "/result.xlsx",
            "approve": root + "/approve",
        },
    }
