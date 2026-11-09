# src/operator_gateway.py
"""Loopback reverse proxy для SSH-входа оператора; собственная private сеть, без Docker socket."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from src.config import Settings
from src.presentation.gateway_security import GATEWAY_HEADER, PROOF_PATHS, issue_gateway_proof
from src.presentation.security import COOKIE

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def forwarded_headers(request: Request, settings) -> list[tuple[str, str]]:
    """Удаляет пользовательский proof и proxy-заголовки; создаёт собственный ограниченный proof."""
    blocked = HOP_HEADERS | {
        part.strip().lower() for part in request.headers.get("connection", "").split(",")
    }
    blocked |= {GATEWAY_HEADER.lower(), "forwarded", "x-real-ip"}
    headers = [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in blocked and not name.lower().startswith("x-forwarded-")
    ]
    if (request.method, request.url.path) in PROOF_PATHS:
        headers.append(
            (
                GATEWAY_HEADER,
                issue_gateway_proof(
                    settings,
                    request.method,
                    request.url.path,
                    request.cookies.get(COOKIE, ""),
                    request.headers.get("origin", ""),
                    request.headers.get("host", ""),
                ),
            )
        )
    return headers


def create_gateway(
    settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> FastAPI:
    """Собирает небольшой gateway из того же image, используя только существующий SESSION_SECRET."""
    settings = settings or Settings()
    if len(settings.session_secret.get_secret_value()) < 24:
        raise ValueError("Для SSH gateway требуется настроенный SESSION_SECRET")
    owned_client = client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Закрывает собственный HTTP pool при остановке только gateway процесса."""
        yield
        if owned_client:
            await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(request: Request):
        """Проксирует только фиксированный API origin, сохраняя поток и несколько Set-Cookie."""
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse(
                {
                    "error": {
                        "code": "gateway_host_forbidden",
                        "message": "Откройте localhost через SSH-туннель",
                    }
                },
                403,
            )
        content = bytearray()
        try:
            async with asyncio.timeout(settings.http_timeout_seconds):
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > settings.max_result_bytes:
                        return JSONResponse(
                            {"error": {"code": "upload_limit", "message": "Превышен размер JSON"}},
                            413,
                        )
        except TimeoutError:
            return JSONResponse(
                {"error": {"code": "upload_timeout", "message": "Истекло время загрузки"}}, 408
            )
        raw_path = request.scope.get("raw_path", b"/")
        if request.scope.get("query_string"):
            raw_path += b"?" + request.scope["query_string"]
        target = httpx.URL(settings.operator_gateway_base_url).copy_with(raw_path=raw_path)
        outgoing = client.build_request(
            request.method,
            target,
            content=bytes(content),
            headers=forwarded_headers(request, settings),
            timeout=settings.http_timeout_seconds,
        )
        try:
            response = await client.send(outgoing, stream=True)
        except httpx.HTTPError:
            return JSONResponse(
                {"error": {"code": "gateway_unavailable", "message": "API временно недоступен"}},
                502,
            )
        blocked = HOP_HEADERS | {
            part.strip().lower() for part in response.headers.get("connection", "").split(",")
        }
        result = StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            background=BackgroundTask(response.aclose),
        )
        result.raw_headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.decode("ascii").lower() not in blocked
        ]
        return result

    return app
