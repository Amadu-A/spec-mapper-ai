# src/presentation/operator_security.py
"""Короткие operator cookies и CSRF; административный ключ никогда не выдаётся браузеру."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Request, Security
from fastapi.security import APIKeyCookie
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.domain.models import MapperError
from src.presentation.security import admin_owner, catalog_key, constant_equals, csrf_token

OPERATOR_COOKIE = "spec_mapper_operator"
operator_cookie = APIKeyCookie(
    name=OPERATOR_COOKIE, auto_error=False, scheme_name="OperatorSession"
)


def operator_serializer(settings) -> URLSafeTimedSerializer:
    """Отделяет подпись операторской сессии от обычной web-cookie прежнего mapper."""
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(), salt="mapper-operator-v1"
    )


def secure_operator_channel(request: Request) -> bool:
    """Разрешает HTTPS или localhost SSH-туннель; Forwarded заголовки не считаются доверенными."""
    if request.url.scheme == "https" or request.url.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    public = urlsplit(request.app.state.container.settings.app_public_base_url)
    return (
        public.scheme == "https"
        and request.headers.get("host") == public.netloc
        and (
            request.method in {"GET", "HEAD"}
            or request.headers.get("Origin") == public.geturl().rstrip("/")
        )
    )


async def operator_nonce(request: Request) -> str | None:
    """Проверяет подпись, фиксированный срок и серверный отзыв; web/API-key не повышает роль."""
    container = request.app.state.container
    try:
        nonce = operator_serializer(container.settings).loads(
            request.cookies.get(OPERATOR_COOKIE, ""),
            max_age=container.settings.operator_session_seconds,
        )
    except BadSignature:
        return None
    if not isinstance(nonce, str) or not await container.operator_access.valid(nonce):
        return None
    return nonce


def check_operator_csrf(request: Request, nonce: str) -> None:
    """Проверяет точный Origin и отдельный CSRF, не совместимый с обычной web-сессией."""
    settings = request.app.state.container.settings
    origins = {str(request.base_url).rstrip("/"), settings.app_public_base_url.rstrip("/")}
    if request.headers.get("Origin") not in origins or not constant_equals(
        request.headers.get("X-Operator-CSRF", ""), csrf_token("operator:" + nonce, settings)
    ):
        raise MapperError("csrf", "Origin или operator CSRF не совпадает", 403)


async def operator_owner(
    request: Request,
    key: Annotated[str | None, Security(catalog_key)],
    cookie: Annotated[str | None, Security(operator_cookie)],
) -> str:
    """Сохраняет X-Catalog-Key для серверных клиентов; браузер использует короткую cookie."""
    if key is not None:
        return admin_owner(request, key)
    nonce = await operator_nonce(request)
    if nonce is None:
        raise MapperError("operator_required", "Требуется операторская сессия", 403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not secure_operator_channel(request):
            raise MapperError(
                "secure_channel_required", "Используйте HTTPS или localhost SSH-туннеля", 403
            )
        check_operator_csrf(request, nonce)
    return "operator"
