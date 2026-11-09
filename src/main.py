# src/main.py
"""ASGI приложение; lifecycle клиентов, thin routers, обработка ошибок и Jinja web."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.bootstrap import Container, build_container
from src.config import Settings
from src.domain.models import MapperError
from src.observability import configure_logging
from src.presentation.dashboard_routes import router as dashboard_router
from src.presentation.middleware import LimitsMiddleware
from src.presentation.routes import router
from src.presentation.security import COOKIE, new_session

ROOT = Path(__file__).resolve().parent


def create_app(container: Container | None = None) -> FastAPI:
    """Собирает production приложение либо принимает тестовый composition root."""
    if container is None:
        settings = Settings()
        settings.require_secrets()
        configure_logging(settings.log_level)
        container = build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Закрывает клиентские ресурсы процесса при штатном завершении API."""
        yield
        await container.close()

    app = FastAPI(
        title="Spec Mapper AI",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        servers=[{"url": container.settings.app_public_base_url}],
        description=(
            "Вторая ступень обработки спецификаций: "
            "JSON → Top-5 → ручной выбор → утверждение для 1С."
        ),
    )
    app.state.container = container
    app.add_middleware(
        LimitsMiddleware,
        max_bytes=container.settings.max_result_bytes,
        per_minute=container.settings.rate_limit_per_minute,
        timeout=container.settings.http_timeout_seconds,
    )
    templates = Jinja2Templates(directory=ROOT / "web" / "templates")
    app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
    app.include_router(router)
    app.include_router(dashboard_router)

    @app.get("/docs", include_in_schema=False)
    async def api_docs(request: Request):
        """Отображает локальную страницу OpenAPI без CDN и без передачи серверных ключей."""
        return templates.TemplateResponse(request=request, name="api_docs.html", context={})

    @app.exception_handler(MapperError)
    async def expected_error(request: Request, error: MapperError) -> JSONResponse:
        """Отображает предметную ошибку без credentials и confidential payload."""
        details = {"code": error.code, "message": error.message}
        headers = {}
        if error.retry_after is not None:
            details["retry_after"] = error.retry_after
            headers["Retry-After"] = str(error.retry_after)
        return JSONResponse({"error": details}, error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """Убирает input из validation diagnostics, чтобы не возвращать целый документ или ключ."""
        return JSONResponse(
            {
                "error": {
                    "code": "validation",
                    "message": "Проверьте типы и обязательные поля запроса",
                }
            },
            422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        """Не позволяет тексту драйверов с credentials попасть в публичный ответ."""
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "Внутренняя ошибка сервиса"}}, 500
        )

    @app.get("/health/live")
    async def live() -> dict:
        """Проверяет только жизнеспособность процесса без ложного утверждения
        готовности зависимостей.
        """
        return {"live": True}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        """Проверяет PostgreSQL, RabbitMQ, embedding, Qdrant и импортированный каталог."""
        status = await container.ready()
        return JSONResponse(status, 200 if status["ready"] else 503)

    async def web_page(request: Request):
        """Сохраняет owner между настоящими страницами и прежними ссылками на job."""
        _, cookie = new_session(request)
        view = request.url.path.strip("/") or "home"
        titles = {
            "home": "Главная",
            "mapping": "Сопоставление",
            "jobs": "Задания",
            "catalog": "Справочник номенклатуры",
            "settings": "Настройки",
            "integrations": "API и интеграция",
            "events": "Журнал событий",
            "operator": "Доступ оператора",
            "help": "Справка",
        }
        if view == "home" and (
            "job" in request.query_params or "source_job_id" in request.query_params
        ):
            response = RedirectResponse("/mapping?" + request.url.query, status_code=307)
        else:
            response = templates.TemplateResponse(
                request=request,
                name=view + ".html",
                context={
                    "view": view,
                    "title": titles[view],
                    "app_version": app.version,
                    "subtitle": "Состояние сервиса, справочника и заданий на сопоставление",
                },
            )
        response.set_cookie(
            COOKIE,
            cookie,
            httponly=True,
            secure=container.settings.cookie_secure,
            samesite="strict",
            max_age=7200,
        )
        return response

    for path in (
        "/",
        "/mapping",
        "/jobs",
        "/catalog",
        "/settings",
        "/integrations",
        "/events",
        "/operator",
        "/help",
    ):
        app.add_api_route(path, web_page, methods=["GET"], include_in_schema=False)

    @app.get("/integration-guide", include_in_schema=False)
    async def integration_guide():
        """Отдаёт версионируемую инструкцию интеграции без секретов сервера."""
        return FileResponse(
            ROOT.parent / "docs" / "INTEGRATION_1C.md",
            media_type="text/markdown",
            filename="INTEGRATION_1C.md",
        )

    return app
