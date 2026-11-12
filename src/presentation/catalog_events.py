# src/presentation/catalog_events.py
"""Защищённый webhook 1С и отдельная авторизованная диагностика durable inbox."""

from datetime import timedelta
from ipaddress import ip_address, ip_network
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.domain.models import MapperError
from src.presentation.operator_security import operator_owner
from src.presentation.security import constant_equals, owner

router = APIRouter(prefix="/api/v1/catalog")
webhook_key = APIKeyHeader(name="X-Webhook-Key", scheme_name="CatalogWebhookKey", auto_error=False)


class CatalogChange(BaseModel):
    """Событие содержит только реальный GUID и операцию; URL/credentials не принимаются."""

    model_config = ConfigDict(extra="forbid")
    guid: UUID
    action: Literal["upsert", "delete"]


class CatalogEvent(BaseModel):
    """Стабильный event_id и UTC время фиксируются до асинхронного получения карточек."""

    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    occurred_at: AwareDatetime
    changes: list[CatalogChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_changes(self):
        """Один GUID не имеет двух действий внутри одной атомарной доставки."""
        if self.occurred_at.utcoffset() != timedelta(0) or len(
            {c.guid for c in self.changes}
        ) != len(self.changes):
            raise ValueError("Нужны UTC время и неповторяющиеся GUID")
        if not self.event_id.int or any(not change.guid.int for change in self.changes):
            raise ValueError("Пустой UUID запрещён")
        return self


class EventReceipt(BaseModel):
    """Подтверждение durable commit, ещё не обещающее применение каталога."""

    event_id: UUID
    state: Literal["pending", "processing", "applied", "failed"]
    duplicate: bool


class EventStatus(BaseModel):
    """Публичные агрегаты без идентификаторов карточек и server-only ключа."""

    received: int
    pending: int
    applied: int
    failed: int
    last_applied_at: float | None
    state: Literal["ready", "awaiting_contract", "awaiting_bootstrap", "awaiting_configuration"]
    webhook_url: str
    secret_configured: bool
    batch_window_seconds: int


class EventDelivery(BaseModel):
    """Строка операторского журнала без полного тела webhook."""

    event_id: UUID
    received_at: float
    state: Literal["pending", "processing", "applied", "failed"]
    attempts: int
    last_error: str | None
    applied_at: float | None
    catalog_version: str | None
    change_count: int


class EventJournal(EventStatus):
    """Последние 50 технических доставок, доступные только оператору."""

    items: list[EventDelivery]


def authenticated_webhook(request: Request, key: Annotated[str | None, Security(webhook_key)]):
    """Отдельный ключ и необязательная IP allowlist; forwarded headers не доверяются."""
    settings = request.app.state.container.settings
    expected = settings.onec_catalog_webhook_key.get_secret_value()
    if not expected:
        raise MapperError(
            "webhook_not_configured", "Webhook ожидает серверный ключ", 503, retry_after=60
        )
    if not key or not constant_equals(key, expected):
        raise MapperError("webhook_unauthorized", "Неверный ключ webhook", 401)
    if settings.onec_webhook_allowed_networks:
        try:
            address = ip_address(request.client.host)
            allowed = any(
                address in ip_network(net) for net in settings.onec_webhook_allowed_networks
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise MapperError("webhook_address_forbidden", "Источник webhook не разрешён", 403)


@router.post(
    "/events",
    status_code=202,
    response_model=EventReceipt,
    responses={
        200: {"description": "Повторная доставка", "model": EventReceipt},
        409: {"description": "Изменённый payload того же event_id"},
        429: {"description": "Повторить после Retry-After"},
        503: {"description": "Inbox временно недоступен"},
    },
)
async def receive_event(
    request: Request,
    body: CatalogEvent,
    authenticated: Annotated[None, Depends(authenticated_webhook)],
):
    """Возвращает 202 только после commit inbox; не ожидает HTTP 1С, RabbitMQ или Qdrant."""
    payload = {
        "event_id": str(body.event_id),
        "occurred_at": body.occurred_at.timestamp(),
        "changes": [{"guid": str(c.guid), "action": c.action} for c in body.changes],
    }
    try:
        result, duplicate = await request.app.state.container.catalog_events.repository.receive(
            payload
        )
    except MapperError:
        raise
    except Exception as exc:
        raise MapperError(
            "event_inbox_unavailable",
            "Не удалось надёжно сохранить событие; повторите доставку",
            503,
            retry_after=5,
        ) from exc
    return JSONResponse(
        {"event_id": result["event_id"], "state": result["state"], "duplicate": duplicate},
        status_code=200 if duplicate else 202,
    )


@router.get("/event-status", response_model=EventStatus)
async def event_summary(request: Request, current: Annotated[str, Depends(owner)]):
    """Публичные пользователям агрегаты без технических GUID карточек."""
    return await request.app.state.container.catalog_events.summary()


@router.get("/events", response_model=EventJournal)
async def event_journal(request: Request, current: Annotated[str, Depends(operator_owner)]):
    """Журнал последних 50 доставок доступен только оператору."""
    return await request.app.state.container.catalog_events.summary(journal=True)
