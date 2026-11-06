# src/presentation/routes.py
"""Тонкие маршруты API и web; все изменения вызывают общие application операции."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from src.application.mappings import public_status, result_page
from src.application.reports import export_json, export_xlsx
from src.domain.models import MapperError
from src.presentation.schemas import MappingStatus, Selection, SourceDocument, SubmitMapping
from src.presentation.security import COOKIE, admin_owner, csrf_token, new_session, owner

router = APIRouter(prefix="/api/v1")
Owner = Annotated[str, Depends(owner)]
Admin = Annotated[str, Depends(admin_owner)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


@router.get("/session")
async def session(request: Request) -> Response:
    """Выдаёт CSRF текущей сессии; серверные интеграционные ключи в браузер не попадают."""
    session_id, cookie = new_session(request)
    settings = request.app.state.container.settings
    response = JSONResponse({"csrf": csrf_token(session_id, settings)})
    response.set_cookie(
        COOKIE,
        cookie,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=7200,
    )
    return response


@router.post("/mappings", response_model=MappingStatus, status_code=202)
async def submit(request: Request, body: SubmitMapping, current: Owner, key: Key) -> dict:
    """Сохраняет durable задание ожидания spec-extracting-ai; повтор безопасен по ключу."""
    job = await request.app.state.container.mappings.submit(current, key, str(body.source_job_id))
    return public_status(job)


@router.post("/mappings/import-json", response_model=MappingStatus, status_code=202)
async def import_json(request: Request, body: SourceDocument, current: Owner, key: Key) -> dict:
    """Принимает окончательный JSON через ту же прикладную очередь, что и API polling."""
    job = await request.app.state.container.mappings.import_document(
        current, key, body.model_dump()
    )
    return public_status(job)


@router.get("/mappings", response_model=list[MappingStatus])
async def jobs(request: Request, current: Owner) -> list[dict]:
    """Показывает только историю текущей cookie-сессии либо интеграционного ключа."""
    return [public_status(job) for job in await request.app.state.container.jobs.list(current)]


@router.get("/mappings/{job_id}", response_model=MappingStatus)
async def status(request: Request, job_id: UUID, current: Owner) -> dict:
    """Возвращает статус, totals и TTL с проверкой owner до доступа к данным."""
    return public_status(await request.app.state.container.jobs.get(str(job_id), current))


@router.get("/mappings/{job_id}/result")
async def result(
    request: Request,
    job_id: UUID,
    current: Owner,
    q: str = Query("", max_length=200),
    status: Literal["all", "matched", "review", "not_found", "manual"] = "all",
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    """Возвращает страницу результата и totals всего задания; partial не считается результатом."""
    job = await request.app.state.container.mappings.result(current, str(job_id))
    return result_page(job, q, status, offset, limit)


@router.get("/mappings/{job_id}/candidates")
async def candidates(
    request: Request, job_id: UUID, current: Owner, row_index: int = Query(ge=0)
) -> dict:
    """Показывает исходные Top-K с cosine-score, рангом и причинами технических конфликтов."""
    job = await request.app.state.container.mappings.result(current, str(job_id))
    rows = job["data"]["rows"]
    if row_index >= len(rows):
        raise MapperError("row_not_found", "Строка не найдена", 404)
    return {"row_index": row_index, "candidates": rows[row_index]["candidates"]}


@router.put("/mappings/{job_id}/rows/{row_index}/selection", response_model=MappingStatus)
async def select(
    request: Request, job_id: UUID, row_index: int, body: Selection, current: Owner
) -> dict:
    """Атомарно сохраняет ручной выбор; null означает подтверждённое отсутствие подходящего."""
    if row_index < 0:
        raise MapperError("row_not_found", "Строка не найдена", 404)
    job = await request.app.state.container.mappings.select(
        current,
        str(job_id),
        row_index,
        str(body.catalog_item_guid) if body.catalog_item_guid else None,
        body.revision,
    )
    return public_status(job)


@router.get("/mappings/{job_id}/source.json")
async def original(request: Request, job_id: UUID, current: Owner) -> Response:
    """Отдаёт только исходный пятиколоночный документ без добавленных полей сопоставления."""
    job = await request.app.state.container.mappings.result(current, str(job_id))
    return JSONResponse(
        job["data"]["source"], headers={"Content-Disposition": 'attachment; filename="source.json"'}
    )


@router.get("/mappings/{job_id}/result.json")
async def json_report(request: Request, job_id: UUID, current: Owner) -> Response:
    """Экспортирует весь результат независимо от фильтра и страницы таблицы."""
    job = await request.app.state.container.mappings.result(current, str(job_id))
    return JSONResponse(
        export_json(job), headers={"Content-Disposition": 'attachment; filename="mapping.json"'}
    )


@router.get("/mappings/{job_id}/result.xlsx")
async def xlsx_report(request: Request, job_id: UUID, current: Owner) -> Response:
    """Строит Excel вне event loop, сохраняя исходные строки literal text."""
    job = await request.app.state.container.mappings.result(current, str(job_id))
    content = await asyncio.to_thread(export_xlsx, job)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="mapping.xlsx"'},
    )


@router.get("/catalog/status")
async def catalog_status(request: Request, current: Owner) -> dict:
    """Возвращает состояние индекса, fingerprint, предупреждения и last_successful_sync."""
    return await request.app.state.container.catalog.status()


@router.get("/catalog/items")
async def search_catalog(
    request: Request,
    current: Owner,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """Ищет любую действующую позицию по имени, коду и артикулу, независимо от Top-K."""
    return {"items": await request.app.state.container.catalog.search(q, limit)}


@router.post("/catalog/imports", response_model=MappingStatus, status_code=202)
async def import_catalog(request: Request, current: Admin, key: Key) -> dict:
    """Инициирует импорт только из настроенного data/; пользовательский путь не принимается."""
    return public_status(await request.app.state.container.jobs.create(current, key, {}, "catalog"))


@router.get("/catalog/imports/{job_id}", response_model=MappingStatus)
async def catalog_import_status(request: Request, job_id: UUID, current: Admin) -> dict:
    """Возвращает статус импорта только владельцу привилегированного credential."""
    return public_status(await request.app.state.container.jobs.get(str(job_id), current))
