# src/presentation/specifications.py
"""Единый REST-контракт спецификаций; SQL и последовательность сервисов скрыты в use-cases."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from src.application.reports import export_json, export_xlsx
from src.application.specifications import specification_export, specification_status
from src.domain.models import MapperError
from src.presentation.routes import Key, Owner
from src.presentation.schemas import Approval, CandidatePage, ExportRow, MappingRow, Selection

router = APIRouter(prefix="/api/v1/specifications", tags=["Спецификации PDF"])


class SpecificationStatus(BaseModel):
    """Состояние одного процесса без ID, kind и внутренних маршрутов извлечения."""

    job_id: UUID
    status: Literal["queued", "extracting", "matching", "awaiting_review", "approved", "failed"]
    created_at: float
    expires_at: float
    revision: int
    approved_at: float | None
    approved_revision: int | None
    error_code: str | None
    total: int
    processed: int
    unresolved_count: int
    counts: dict[str, int]
    catalog_version: str | None
    links: dict[str, str]


class SpecificationRows(SpecificationStatus):
    """Страница строк для проверки с общей ревизией и стабильными индексами с нуля."""

    filtered_total: int
    offset: int
    items: list[MappingRow]
    algorithm_version: str
    source_hash: str
    file_name: str


class SpecificationApproval(BaseModel):
    """Публичные сведения утверждения без внутреннего owner другой подсистемы."""

    approved_at: float
    approved_revision: int
    catalog_version: str


class SpecificationExport(BaseModel):
    """Полный экспорт единого процесса с одним ID и сохранёнными исходными строками."""

    job_id: UUID
    file_name: str
    source_hash: str
    catalog_version: str
    algorithm_version: str
    status: Literal["awaiting_review", "approved"]
    approved_at: float | None
    approved_revision: int | None
    items: list[ExportRow]
    approval: SpecificationApproval | None = None


class ApprovedSpecification(SpecificationExport):
    """Итоговый контракт загрузки в 1С требует immutable утверждённый снимок."""

    status: Literal["approved"]
    approved_at: float
    approved_revision: int
    approval: SpecificationApproval


@router.post("", status_code=202, response_model=SpecificationStatus)
async def submit(
    request: Request,
    current: Owner,
    key: Key,
    file: Annotated[UploadFile, File(description="PDF спецификации")],
    pages: Annotated[str, Form(min_length=1, max_length=2000)],
    project_code: Annotated[str, Form(min_length=1, max_length=200)],
) -> dict:
    """Принимает PDF в durable хранилище и возвращает ID без ожидания LLM/подбора."""
    container = request.app.state.container
    content = await file.read(container.settings.max_pdf_bytes + 1)
    try:
        job = await container.specifications.submit(
            current, key, content, file.filename or "specification.pdf", pages, project_code
        )
        return specification_status(job)
    finally:
        await file.close()


@router.get("/{job_id}", response_model=SpecificationStatus)
async def status(request: Request, job_id: UUID, current: Owner) -> dict:
    """Возвращает состояние всего процесса и диагностический код при отказе."""
    return specification_status(
        await request.app.state.container.specifications.get(current, str(job_id))
    )


@router.get("/{job_id}/rows", response_model=SpecificationRows, response_model_exclude_unset=True)
async def rows(
    request: Request,
    job_id: UUID,
    current: Owner,
    q: str = Query("", max_length=200),
    status: Literal["all", "matched", "review", "not_found", "manual"] = "all",
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    """Выдаёт готовые строки для проверки; незавершённый результат возвращает 409."""
    return await request.app.state.container.specifications.rows(
        current, str(job_id), q, status, offset, limit
    )


@router.get("/{job_id}/candidates", response_model=CandidatePage, response_model_exclude_unset=True)
async def candidates(
    request: Request, job_id: UUID, current: Owner, row_index: int = Query(ge=0)
) -> dict:
    """Возвращает сохранённые варианты для строки без повторного вызова модели."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    job = await c.mappings.result(current, str(job_id))
    if row_index >= len(job["data"]["rows"]):
        raise MapperError("row_not_found", "Строка не найдена", 404)
    return {"row_index": row_index, "candidates": job["data"]["rows"][row_index]["candidates"]}


@router.put("/{job_id}/rows/{row_index}/selection", response_model=SpecificationStatus)
async def select(
    request: Request, job_id: UUID, row_index: int, body: Selection, current: Owner
) -> dict:
    """Сохраняет выбор или явный null с общей проверкой владельца, TTL и ревизии."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    job = await c.mappings.select(
        current,
        str(job_id),
        row_index,
        str(body.catalog_item_guid) if body.catalog_item_guid else None,
        body.revision,
    )
    return specification_status(job)


@router.post("/{job_id}/approve", response_model=SpecificationStatus)
async def approve(request: Request, job_id: UUID, body: Approval, current: Owner, key: Key) -> dict:
    """Атомарно утверждает текущие проверенные выборы общим механизмом immutable snapshot."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    return specification_status(await c.mappings.approve(current, str(job_id), body.revision, key))


@router.get("/{job_id}/result.json", response_model=ApprovedSpecification)
async def result_json(request: Request, job_id: UUID, current: Owner) -> JSONResponse:
    """Выдаёт исключительно утверждённый JSON, пригодный для переноса в 1С."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    snapshot = await c.jobs.approved(str(job_id), current)
    return JSONResponse(
        specification_export(snapshot),
        headers={"Content-Disposition": 'attachment; filename="specification.json"'},
    )


@router.get("/{job_id}/draft.json", response_model=SpecificationExport)
async def draft_json(request: Request, job_id: UUID, current: Owner) -> JSONResponse:
    """Отдаёт полный предварительный результат для просмотра до утверждения."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    job = await c.mappings.result(current, str(job_id))
    return JSONResponse(specification_export(export_json(job)))


@router.get(
    "/{job_id}/result.xlsx",
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}},
        409: {"description": "Результат ещё не утверждён"},
    },
)
async def result_xlsx(request: Request, job_id: UUID, current: Owner) -> Response:
    """Формирует Excel из того же утверждённого снимка, что итоговый JSON."""
    c = request.app.state.container
    await c.specifications.get(current, str(job_id))
    snapshot = await c.jobs.approved(str(job_id), current)
    content = await asyncio.to_thread(export_xlsx, snapshot)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="specification.xlsx"'},
    )
