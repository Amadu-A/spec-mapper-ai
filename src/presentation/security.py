# src/presentation/security.py
"""Изолированные подписанные cookie, отдельные API credentials и CSRF/Origin для web writes."""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated
from uuid import uuid4

from fastapi import Request, Security
from fastapi.security import APIKeyHeader
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.domain.models import MapperError

COOKIE = "spec_mapper_session"
integration_key = APIKeyHeader(name="X-API-Key", scheme_name="IntegrationKey", auto_error=False)
catalog_key = APIKeyHeader(name="X-Catalog-Key", scheme_name="CatalogKey", auto_error=False)


def constant_equals(left: str, right: str) -> bool:
    """Сравнивает UTF-8 bytes за постоянное время, отклоняя неверные не-ASCII headers без 500."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def serializer(settings) -> URLSafeTimedSerializer:
    """Использует проектный секрет и собственный salt, не совместимый с upstream cookie."""
    return URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt="spec-mapper-v1")


def csrf_token(session_id: str, settings) -> str:
    """Привязывает CSRF к конкретной cookie-сессии, не раскрывая signing secret."""
    return hmac.new(
        settings.session_secret.get_secret_value().encode(),
        ("csrf:" + session_id).encode(),
        hashlib.sha256,
    ).hexdigest()


def read_session(request: Request) -> str | None:
    """Отклоняет изменённую или истёкшую cookie без создания пользовательского профиля."""
    try:
        return serializer(request.app.state.container.settings).loads(
            request.cookies.get(COOKIE, ""), max_age=7200
        )
    except BadSignature:
        return None


def new_session(request: Request) -> tuple[str, str]:
    """Возобновляет локальную сессию либо выдаёт новую случайную изолированную cookie."""
    session_id = read_session(request) or str(uuid4())
    return session_id, serializer(request.app.state.container.settings).dumps(session_id)


def owner(request: Request, key: Annotated[str | None, Security(integration_key)]) -> str:
    """Разделяет API и cookie владельцев; cookie writes требуют совпадения Origin и CSRF."""
    settings = request.app.state.container.settings
    if key is not None:
        if settings.api_key.get_secret_value() and constant_equals(
            key, settings.api_key.get_secret_value()
        ):
            return "api:" + hashlib.sha256(key.encode()).hexdigest()
        matched = None
        for name, secret in settings.api_keys.items():
            if constant_equals(key, secret.get_secret_value()):
                matched = name
        if matched is None:
            raise MapperError("unauthorized", "Неверный API-ключ", 401)
        return "api:" + matched
    session_id = read_session(request)
    if session_id is None:
        raise MapperError("unauthorized", "Откройте web-интерфейс для создания сессии", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin", "")
        expected_origins = {
            str(request.base_url).rstrip("/"),
            settings.app_public_base_url.rstrip("/"),
        }
        if origin not in expected_origins or not constant_equals(
            request.headers.get("X-CSRF-Token", ""), csrf_token(session_id, settings)
        ):
            raise MapperError("csrf", "Origin или CSRF-токен не совпадает с web-сессией", 403)
    return "web:" + session_id


def admin_owner(request: Request, key: Annotated[str | None, Security(catalog_key)]) -> str:
    """Импорт каталога разрешён только отдельному project credential, без web admin панели."""
    settings = request.app.state.container.settings
    key = key or ""
    secret = settings.catalog_admin_key.get_secret_value()
    if not secret or not constant_equals(key, secret):
        raise MapperError("forbidden", "Нужен отдельный ключ импорта каталога", 403)
    return "admin:" + hashlib.sha256(key.encode()).hexdigest()
