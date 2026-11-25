# src/presentation/invoices.py
"""Тонкий REST invoice-профиля 8087; общие auth/CSRF/CAS/TTL, без бизнес-логики в router."""

import asyncio
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from src.application.invoice_reports import invoice_export, invoice_xlsx
from src.application.invoices.ports import Upload
from src.application.invoices.service import invoice_status
from src.domain.invoices.models import InvoiceExport
from src.domain.models import MapperError
from src.presentation.invoice_schemas import (
    ApprovedInvoice,
    InvoiceApproval,
    InvoiceCandidates,
    InvoiceMapping,
    InvoiceRows,
    InvoiceStatus,
)
from src.presentation.routes import Key, Owner
from src.presentation.schemas import Selection

router = APIRouter(
    prefix="/api/v1/invoices",
    tags=["Сопоставление счетов"],
    responses={
        401: {"description": "Требуется API-ключ или web-сессия"},
        403: {"description": "Origin/CSRF не совпадают"},
        404: {"description": "Нет задания этого профиля/владельца"},
        410: {"description": "Срок хранения задания истёк"},
        409: {"description": "Результат не готов/не утверждён, revision или финансовое согласие"},
        413: {"description": "Превышен лимит файла, пакета или выбранных страниц"},
        415: {"description": "MIME не соответствует PDF/JPEG/PNG"},
        422: {"description": "Неверные страницы, повреждённый файл или invoice_extraction.v1"},
        429: {"description": "Превышена частота запросов; Retry-After"},
    },
)


@router.post("", status_code=202, response_model=InvoiceStatus)
async def submit(
    request: Request,
    current: Owner,
    key: Key,
    files: Annotated[
        list[UploadFile], File(description="1–10 PDF/JPEG/PNG; 50 MiB/файл, 100 MiB/пакет")
    ],
    page_selection: Annotated[
        str, Form(description='JSON: {"0":"1-3","1":"2"}; иначе все страницы')
    ] = "{}",
) -> dict:
    """202 после атомарного сохранения всего пакета и outbox, без синхронного ожидания VLM."""
    try:
        try:
            selection = json.loads(page_selection)
        except ValueError as exc:
            raise MapperError("invalid_pages", "Ожидается JSON выбора страниц", 422) from exc
        uploads = []
        for file in files:

            async def chunks(file=file):
                """Передаёт ограниченный spool use-case блоками по одному MiB."""
                while chunk := await file.read(1024 * 1024):
                    yield chunk

            uploads.append(
                Upload(
                    file.filename or "document",
                    file.content_type or "application/octet-stream",
                    chunks(),
                )
            )
        return invoice_status(
            await request.app.state.container.invoices.submit(current, key, uploads, selection)
        )
    finally:
        for file in files:
            await file.close()


@router.post("/import-json", status_code=202, response_model=InvoiceStatus)
async def import_json(request: Request, body: InvoiceExport, current: Owner, key: Key) -> dict:
    """Сохраняет полный JSON 8083 без defaults, нормализации или повторного извлечения."""
    return invoice_status(
        await request.app.state.container.invoices.import_document(
            current, key, body.model_dump(exclude_unset=True)
        )
    )


@router.get("/{job_id}", response_model=InvoiceStatus)
async def status(request: Request, job_id: UUID, current: Owner) -> dict:
    """Возвращает единый статус без upstream ID, URL и исходных реквизитов."""
    return invoice_status(await request.app.state.container.invoices.get(current, str(job_id)))


@router.get("/{job_id}/rows", response_model=InvoiceRows, response_model_exclude_unset=True)
async def rows(
    request: Request,
    job_id: UUID,
    current: Owner,
    q: str = Query("", max_length=200),
    status: Literal["all", "matched", "review", "not_found", "manual", "not_applicable"] = "all",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    """Читает сохранённую страницу с финансами, предупреждениями и revision."""
    return await request.app.state.container.invoices.rows(
        current, str(job_id), q, status, offset, limit
    )


@router.get("/{job_id}/candidates", response_model=InvoiceCandidates)
async def candidates(
    request: Request, job_id: UUID, current: Owner, row_index: int = Query(ge=0)
) -> dict:
    """Отдаёт сохранённый Top-5; не вызывает embedding/поиск повторно."""
    job = await request.app.state.container.invoices.mappings.result(current, str(job_id))
    if row_index >= len(job["data"]["rows"]):
        raise MapperError("row_not_found", "Строка не найдена", 404)
    return {
        "row_index": row_index,
        "candidates": job["data"]["rows"][row_index]["candidates"],
        "catalog_version": job["data"]["catalog_version"],
        "algorithm_version": job["data"]["algorithm_version"],
    }


@router.put("/{job_id}/rows/{row_index}/selection", response_model=InvoiceStatus)
async def select(
    request: Request, job_id: UUID, row_index: int, body: Selection, current: Owner
) -> dict:
    """Сохраняет внутренний GUID или null через существующий механизм выбора/CAS."""
    return invoice_status(
        await request.app.state.container.invoices.select(
            current,
            str(job_id),
            row_index,
            str(body.catalog_item_guid) if body.catalog_item_guid else None,
            body.revision,
        )
    )


@router.delete("/{job_id}/rows/{row_index}/selection", response_model=InvoiceStatus)
async def clear(
    request: Request, job_id: UUID, row_index: int, current: Owner, revision: int = Query(ge=0)
) -> dict:
    """Удаляет только selection, сохраняя исходную строку и Top-5."""
    return invoice_status(
        await request.app.state.container.invoices.select(
            current, str(job_id), row_index, None, revision
        )
    )


@router.post("/{job_id}/approve", response_model=InvoiceStatus)
async def approve(
    request: Request, job_id: UUID, body: InvoiceApproval, current: Owner, key: Key
) -> dict:
    """Утверждает весь результат после явного финансового acknowledgment каждого документа."""
    return invoice_status(
        await request.app.state.container.invoices.approve(
            current, str(job_id), body.revision, key, body.acknowledged_document_warnings
        )
    )


@router.get("/{job_id}/source.json", response_model=InvoiceExport)
async def source(request: Request, job_id: UUID, current: Owner) -> JSONResponse:
    """Возвращает точный сохранённый source без добавленных onec/matching/defaults."""
    job = await request.app.state.container.invoices.get(current, str(job_id))
    if job["data"].get("source") is None:
        raise MapperError("result_not_ready", "Исходный JSON ещё не получен", 409)
    return JSONResponse(
        job["data"]["source"],
        headers={"Content-Disposition": 'attachment; filename="invoice-source.json"'},
    )


@router.get("/{job_id}/draft.json", response_model=InvoiceMapping)
async def draft(request: Request, job_id: UUID, current: Owner) -> JSONResponse:
    """Формирует полный invoice_mapping.v1 для предварительного просмотра."""
    job = await request.app.state.container.invoices.mappings.result(current, str(job_id))
    return JSONResponse(invoice_export(job))


@router.get("/{job_id}/result.json", response_model=ApprovedInvoice)
async def result(request: Request, job_id: UUID, current: Owner) -> JSONResponse:
    """Читает только зафиксированный immutable snapshot; до approve возвращает 409."""
    c = request.app.state.container
    await c.invoices.get(current, str(job_id))
    return JSONResponse(
        await c.jobs.approved(str(job_id), current),
        headers={"Content-Disposition": 'attachment; filename="invoice-mapping.json"'},
    )


@router.get(
    "/{job_id}/result.xlsx",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        }
    },
)
async def excel(request: Request, job_id: UUID, current: Owner) -> Response:
    """Строит финансовую книгу из того же approved JSON, без повторного подбора."""
    c = request.app.state.container
    await c.invoices.get(current, str(job_id))
    content = await asyncio.to_thread(invoice_xlsx, await c.jobs.approved(str(job_id), current))
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="invoice-mapping.xlsx"'},
    )


@router.get(
    "/{job_id}/files/{file_index}/pages/{page}/preview",
    response_class=Response,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def preview(
    request: Request,
    job_id: UUID,
    file_index: int,
    page: int,
    current: Owner,
    width: int = Query(1200, ge=120, le=2400),
) -> Response:
    """Защищённо проксирует preview по одному адресу 8087, не раскрывая ключ/URL 8083."""
    return Response(
        await request.app.state.container.invoices.preview(
            current, str(job_id), file_index, page, width
        ),
        media_type="image/png",
    )
