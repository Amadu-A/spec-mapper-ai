# src/presentation/lifecycle.py
"""Общие маршруты snapshot/events/cancel; bounded SSE только из durable журнала Mapper."""

import asyncio
import json
import re
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from src.domain.models import MapperError
from src.presentation.lifecycle_schemas import LifecyclePage, LifecycleSnapshot
from src.presentation.routes import Owner

router = APIRouter(tags=["Жизненный цикл заданий"])
FINISHED = {"awaiting_review", "approved", "cancelled", "failed"}


def cursor_value(header: str | None, after: str | None) -> int:
    """Last-Event-ID приоритетнее query; ограничивает длину до преобразования bigint."""
    value = header if header is not None else after or "0"
    if not re.fullmatch(r"[0-9]{1,16}", value):
        raise MapperError("invalid_event_cursor", "Некорректный cursor событий", 409)
    return int(value)


async def stream_events(
    request: Request, owner: str, identifier: str, family: str, after: int
) -> StreamingResponse:
    """Проверяет доступ до headers и ограничивает число потоков owner/API process."""
    container = request.app.state.container
    lifecycle = container.lifecycle
    snapshot = await lifecycle.get(owner, identifier, family)
    await lifecycle.events(owner, identifier, family, after)
    slots = request.app.state.lifecycle_streams
    settings = container.settings
    if (
        slots.get(owner, 0) >= settings.lifecycle_sse_per_owner
        or sum(slots.values()) >= settings.lifecycle_sse_total
    ):
        raise MapperError("stream_limit", "Слишком много открытых потоков", 429, retry_after=15)
    slots[owner] = slots.get(owner, 0) + 1
    released = False

    async def release():
        """Освобождает slot ровно один раз при terminal, disconnect или ASGI cancellation."""
        nonlocal released
        if not released:
            released = True
            slots[owner] -= 1
            if not slots[owner]:
                slots.pop(owner)

    async def frames():
        """Не держит SQL connection между кадрами, завершает terminal и соблюдает TTL."""
        cursor, state = after, snapshot["status"]
        deadline, heartbeat = monotonic() + settings.lifecycle_sse_seconds, monotonic()
        try:
            yield "retry: 3000\n\n"
            while monotonic() < deadline and not await request.is_disconnected():
                try:
                    records = await lifecycle.events(owner, identifier, family, cursor)
                except MapperError as error:
                    kind = "access_expired" if error.status in {401, 403, 404, 410} else "reset"
                    yield "event: " + kind + "\ndata: " + json.dumps({"code": error.code}) + "\n\n"
                    return
                for record in records:
                    cursor, state = record["id"], record["data"]["status"]
                    yield f"id: {cursor}\nevent: lifecycle\ndata: " + json.dumps(record) + "\n\n"
                if state in FINISHED and len(records) < 100:
                    return
                if monotonic() - heartbeat >= 15:
                    yield ": keepalive\n\n"
                    heartbeat = monotonic()
                await asyncio.sleep(2)
            if monotonic() >= deadline:
                yield "event: reconnect\ndata: {}\n\n"
        finally:
            await release()

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        background=BackgroundTask(release),
    )


def register(family: str) -> None:
    """Три delivery адаптера используют один lifecycle; алгоритмы профилей не копируются."""
    prefix = "/api/v1/" + family

    async def snapshot(request: Request, job_id: UUID, current: Owner) -> dict:
        """Возвращает безопасный durable snapshot своего профиля."""
        return await request.app.state.container.lifecycle.get(current, str(job_id), family)

    async def cancel(request: Request, job_id: UUID, current: Owner) -> dict:
        """Отменяет свой job без изменения upstream внешнего клиента или approved snapshot."""
        return await request.app.state.container.lifecycle.cancel(current, str(job_id), family)

    async def events(
        request: Request,
        job_id: UUID,
        current: Owner,
        after: str | None = Query(None, max_length=16),
    ) -> StreamingResponse:
        """SSE replay: Last-Event-ID или after, bounded metadata, heartbeat и terminal close."""
        cursor = cursor_value(request.headers.get("Last-Event-ID"), after)
        return await stream_events(request, current, str(job_id), family, cursor)

    async def history(
        request: Request,
        current: Owner,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
        state: str = Query("", max_length=32),
    ) -> dict:
        """Не создаёт jobs при refresh и не раскрывает документы другого owner."""
        return await request.app.state.container.lifecycle.list(
            current, family, offset, limit, state
        )

    common = {
        404: {"description": "Нет доступа к заданию этого профиля"},
        410: {"description": "TTL истёк"},
        409: {"description": "Cursor утрачен или отмена запрещена"},
        429: {"description": "Лимит подписок; соблюдайте Retry-After"},
    }
    for suffix, endpoint, method, model in [
        ("/snapshot", snapshot, "GET", LifecycleSnapshot),
        ("/cancel", cancel, "POST", LifecycleSnapshot),
        ("/events", events, "GET", None),
    ]:
        responses = dict(common)
        if suffix == "/events":
            responses[200] = {"content": {"text/event-stream": {"schema": {"type": "string"}}}}
        router.add_api_route(
            prefix + "/{job_id}" + suffix,
            endpoint,
            methods=[method],
            response_model=model,
            name=family + suffix.replace("/", "_"),
            responses=responses,
        )
    if family != "mappings":
        router.add_api_route(
            prefix,
            history,
            methods=["GET"],
            response_model=LifecyclePage,
            name=family + "_history",
            responses=common,
        )


for family in ("mappings", "specifications", "invoices"):
    register(family)
