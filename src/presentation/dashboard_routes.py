# src/presentation/dashboard_routes.py
"""Тонкие HTTP маршруты dashboard/operator; source/result чужих jobs здесь не выдаются."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from src.application.reports import export_catalog_xlsx
from src.domain.models import MapperError
from src.presentation.dashboard_schemas import (
    CatalogRun,
    ImportHistoryPage,
    JobsPage,
    OperatorLogin,
    SyncConfiguration,
    SyncSettingsView,
)
from src.presentation.operator_security import (
    OPERATOR_COOKIE,
    check_operator_csrf,
    operator_nonce,
    operator_owner,
    operator_serializer,
    secure_operator_channel,
)
from src.presentation.security import csrf_token, owner

router = APIRouter(prefix="/api/v1")
Operator = Annotated[str, Depends(operator_owner)]
Owner = Annotated[str, Depends(owner)]


@router.get("/operator/session")
async def operator_session(request: Request, current: Owner) -> dict:
    """Выдаёт признак роли и её CSRF; ticket, session cookie и API-ключи не возвращаются."""
    nonce = await operator_nonce(request)
    settings = request.app.state.container.settings
    return {
        "operator": nonce is not None,
        "can_login": secure_operator_channel(request),
        "csrf": csrf_token("operator:" + nonce, settings) if nonce else None,
        "session_seconds": settings.operator_session_seconds,
    }


@router.post("/operator/session")
async def operator_login(request: Request, body: OperatorLogin, current: Owner) -> Response:
    """Обменивает ticket только через защищённый канал и с обычным web CSRF."""
    if not secure_operator_channel(request):
        raise MapperError(
            "secure_channel_required", "Откройте HTTPS или localhost через SSH-туннель", 403
        )
    container = request.app.state.container
    nonce = await container.operator_access.consume(body.ticket)
    response = JSONResponse(
        {"operator": True, "csrf": csrf_token("operator:" + nonce, container.settings)}
    )
    response.set_cookie(
        OPERATOR_COOKIE,
        operator_serializer(container.settings).dumps(nonce),
        httponly=True,
        samesite="strict",
        secure=container.settings.cookie_secure or request.url.scheme == "https",
        max_age=container.settings.operator_session_seconds,
    )
    return response


@router.delete("/operator/session")
async def operator_logout(request: Request, current: Operator) -> Response:
    """Отзывает сессию на сервере и удаляет HttpOnly cookie без изменения обычного owner."""
    nonce = await operator_nonce(request)
    if nonce:
        check_operator_csrf(request, nonce)
        await request.app.state.container.operator_access.revoke(nonce)
    response = JSONResponse({"operator": False})
    response.delete_cookie(OPERATOR_COOKIE)
    return response


@router.get("/catalog/sync-settings", response_model=SyncSettingsView)
async def sync_settings(request: Request, current: Operator) -> dict:
    """Оператор видит только безопасные настройки и secret_configured, не секреты."""
    return await request.app.state.container.sync.settings()


@router.put("/catalog/sync-settings", response_model=SyncSettingsView)
async def save_sync_settings(request: Request, body: SyncConfiguration, current: Operator) -> dict:
    """Сохраняет CAS-настройки; сам HTTP запрос не запускает импорт."""
    return await request.app.state.container.sync.save(
        body.model_dump(exclude={"revision"}), body.revision, current
    )


@router.post("/catalog/connection-test", response_model=SyncSettingsView)
async def connection_test(request: Request, current: Operator) -> dict:
    """Реальный probe разрешённого источника без публикации каталога; контракт может
    отсутствовать.
    """
    return await request.app.state.container.sync.connection_test()


@router.get("/catalog/import-history", response_model=ImportHistoryPage)
async def import_history(
    request: Request,
    current: Operator,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    state: str = Query("", max_length=32),
    source_type: str = Query("", max_length=32),
) -> dict:
    """Пагинирует технические отчёты с отдельным сроком хранения; документы сюда не попадают."""
    return await request.app.state.container.sync_repository.history(
        offset, limit, state, source_type
    )


@router.get("/catalog/import-history/{run_id}/report.json", response_model=CatalogRun)
@router.get("/catalog/import-history/{run_id}/report", response_model=CatalogRun)
async def catalog_report(request: Request, run_id: UUID, current: Operator) -> Response:
    """Выгружает фактические агрегаты/события реального run, включая partial/error marker."""
    report = await request.app.state.container.sync_repository.report(str(run_id))
    return JSONResponse(
        report, headers={"Content-Disposition": 'attachment; filename="catalog-import.json"'}
    )


@router.get("/catalog/import-history/{run_id}/report.xlsx")
async def catalog_report_xlsx(request: Request, run_id: UUID, current: Operator) -> Response:
    """Строит технический Excel вне event loop, без confidential XML/наименований."""
    report = await request.app.state.container.sync_repository.report(str(run_id))
    content = await asyncio.to_thread(export_catalog_xlsx, report)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="catalog-import.xlsx"'},
    )


@router.get("/operator/audit")
async def operator_audit(
    request: Request,
    current: Operator,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    event: str = Query("", max_length=80),
) -> dict:
    """Даёт безопасный журнал операторских изменений отдельно от стадий обработки импортов."""
    return await request.app.state.container.sync_repository.audit(offset, limit, event)


@router.get("/operator/jobs", response_model=JobsPage)
async def operator_jobs(
    request: Request,
    current: Operator,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    state: str = Query("", max_length=32),
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    """Глобальный read-only обзор метаданных не даёт право читать/редактировать чужой job."""
    return await request.app.state.container.sync_repository.jobs_page(
        None, offset, limit, state, order
    )


@router.get("/dashboard/jobs", response_model=JobsPage)
async def own_jobs(
    request: Request,
    current: Owner,
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    state: str = Query("", max_length=32),
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    """Обычная сессия/интеграция видит только свои неистёкшие задания с сортировкой/пагинацией."""
    return await request.app.state.container.sync_repository.jobs_page(
        current, offset, limit, state, order
    )


@router.get("/dashboard/summary")
async def dashboard_summary(request: Request, current: Owner) -> dict:
    """Сводит реальные проверки/агрегаты; глобальная статистика доступна только operator cookie."""
    container = request.app.state.container
    operator = await operator_nonce(request) is not None
    health, metrics, settings, catalog = await asyncio.gather(
        container.ready(),
        container.sync_repository.summary(None if operator else current),
        container.sync_repository.settings(),
        container.catalog.status(),
    )
    last_seen = await container.sync_repository.activity("worker")
    worker = (
        last_seen is not None
        and container.jobs.clock() - last_seen <= container.settings.worker_heartbeat_ttl_seconds
    )
    header = {
        "api": "healthy" if health["ready"] else "degraded",
        "worker": "healthy" if worker else "unavailable",
        "postgresql": "healthy" if health["dependencies"].get("database") else "unavailable",
        "qdrant": "healthy" if health["dependencies"].get("qdrant") else "unavailable",
        "embedding": "healthy" if health["dependencies"].get("shared_embedding") else "unavailable",
    }
    return {
        "operator": operator,
        "health": header,
        "worker_last_seen": last_seen,
        "metrics": metrics,
        "catalog": {
            key: catalog.get(key)
            for key in (
                "ready",
                "count",
                "version",
                "source_hash",
                "last_successful_sync",
                "last_error",
                "statistics",
                "warnings",
            )
        },
        "schedule": {
            key: settings[key] for key in ("enabled", "time_utc", "source_type", "next_run_at")
        },
    }


@router.get("/integrations/status")
async def integrations(request: Request, current: Owner) -> dict:
    """HTTP 1С здесь только stored probe; Telegram — last successful inbound, не фиктивный
    connect.
    """
    container = request.app.state.container
    config = await container.sync.settings()
    seen = await container.sync_repository.activity("api:telegram")
    telegram_configured = "telegram" in container.settings.api_keys
    state = "not_configured"
    if not config["contract_confirmed"]:
        state = "awaiting_contract"
    elif config["source_type"] != "http_onec":
        state = "not_configured"
    elif config["probe_current"]:
        state = config["probe"].get("state", "not_configured")
    elif config["url"]:
        state = "check_required"
    return {
        "extracting": await container.integration_probes.extracting(),
        "onec": {
            "state": state,
            "checked_at": config["probe"].get("checked_at"),
            "error_code": config["probe"].get("error_code") or config["configuration_error"],
        },
        "telegram": {
            "configured": telegram_configured,
            "last_seen": seen,
            "state": "key_missing"
            if not telegram_configured
            else (
                "active"
                if seen and container.jobs.clock() - seen < 3600
                else "past_activity"
                if seen
                else "no_activity"
            ),
        },
    }
