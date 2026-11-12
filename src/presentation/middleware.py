# src/presentation/middleware.py
"""Ограничения тела запроса и частоты без записи документов, cookie и ключей в журналы."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from time import monotonic

from starlette.requests import Request
from starlette.responses import JSONResponse


class LimitsMiddleware:
    """Ограничивает фактически полученные bytes до JSON parsing, включая chunked upload."""

    def __init__(
        self,
        app,
        max_bytes: int,
        per_minute: int,
        timeout: int,
        webhook_max_bytes=32768,
        webhook_per_minute=120,
    ) -> None:
        """Задаёт общие лимиты одного API-процесса; reverse proxy не считается доверенным."""
        self.app, self.max_bytes, self.per_minute, self.timeout = (
            app,
            max_bytes,
            per_minute,
            timeout,
        )
        self.requests = defaultdict(deque)
        self.webhook_max_bytes, self.webhook_per_minute = webhook_max_bytes, webhook_per_minute

    async def __call__(self, scope, receive, send) -> None:
        """Проверяет лимиты перед передачей запроса в FastAPI и добавляет защитные заголовки."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = monotonic()
        client = scope.get("client", ("unknown",))[0]
        webhook = scope.get("path") == "/api/v1/catalog/events" and scope["method"] == "POST"
        if webhook:
            client = "webhook:" + client
        maximum = self.webhook_max_bytes if webhook else self.max_bytes
        per_minute = self.webhook_per_minute if webhook else self.per_minute
        if len(self.requests) > 5000:
            self.requests = defaultdict(
                deque, {k: v for k, v in self.requests.items() if v and v[-1] > now - 60}
            )
        bucket = self.requests[client]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        counted = not scope.get("path", "").startswith("/static/")
        if counted and len(bucket) >= per_minute:
            await JSONResponse(
                {"error": {"code": "rate_limit", "message": "Слишком много запросов"}},
                429,
                headers={"Retry-After": "60"},
            )(scope, receive, send)
            return
        if counted:
            bucket.append(now)
        content = bytearray()
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            try:
                async with asyncio.timeout(self.timeout):
                    async for chunk in Request(scope, receive).stream():
                        content.extend(chunk)
                        if len(content) > maximum:
                            await JSONResponse(
                                {
                                    "error": {
                                        "code": "upload_limit",
                                        "message": "Превышен размер JSON",
                                    }
                                },
                                413,
                            )(scope, receive, send)
                            return
            except TimeoutError:
                await JSONResponse(
                    {"error": {"code": "upload_timeout", "message": "Истекло время загрузки"}}, 408
                )(scope, receive, send)
                return

            async def buffered_receive():
                """Передаёт уже ограниченное тело JSON parser без второго чтения сети."""
                return {"type": "http.request", "body": bytes(content), "more_body": False}

            receive = buffered_receive

        response_status = 500

        async def secured_send(message):
            """Запрещает кэш документов и загрузку исполняемых ресурсов с внешних доменов."""
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                message["headers"].extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"),
                        (b"cache-control", b"no-store"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; "
                            b"img-src 'self' data:; frame-ancestors 'none'; "
                            b"base-uri 'self'; form-action 'self'",
                        ),
                    ]
                )
            await send(message)

        await self.app(scope, receive, secured_send)
        integration = scope.get("state", {}).get("integration_name")
        if integration and 200 <= response_status < 300:
            try:
                await scope["app"].state.container.sync_repository.touch("api:" + integration)
            except Exception:
                logging.getLogger(__name__).warning("integration_activity_retry")
