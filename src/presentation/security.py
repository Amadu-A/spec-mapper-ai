# src/presentation/security.py
"""Изолированные подписанные cookie, отдельные API credentials и CSRF/Origin для web writes."""

from __future__ import annotations

import hashlib
import hmac
from uuid import uuid4

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.domain.models import MapperError

COOKIE = "spec_mapper_session"


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


def owner(request: Request) -> str:
    """Разделяет API и cookie владельцев; cookie writes требуют совпадения Origin и CSRF."""
    settings = request.app.state.container.settings
    key = request.headers.get("X-API-Key")
    if key is not None:
        if not settings.api_key.get_secret_value() or not hmac.compare_digest(
            key, settings.api_key.get_secret_value()
        ):
            raise MapperError("unauthorized", "Неверный API-ключ", 401)
        return "api:" + hashlib.sha256(key.encode()).hexdigest()
    session_id = read_session(request)
    if session_id is None:
        raise MapperError("unauthorized", "Откройте web-интерфейс для создания сессии", 401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin", "")
        expected_origin = str(request.base_url).rstrip("/")
        if origin != expected_origin or not hmac.compare_digest(
            request.headers.get("X-CSRF-Token", ""), csrf_token(session_id, settings)
        ):
            raise MapperError("csrf", "Origin или CSRF-токен не совпадает с web-сессией", 403)
    return "web:" + session_id


def admin_owner(request: Request) -> str:
    """Импорт каталога разрешён только отдельному project credential, без web admin панели."""
    settings = request.app.state.container.settings
    key = request.headers.get("X-Catalog-Key", "")
    secret = settings.catalog_admin_key.get_secret_value()
    if not secret or not hmac.compare_digest(key, secret):
        raise MapperError("forbidden", "Нужен отдельный ключ импорта каталога", 403)
    return "admin:" + hashlib.sha256(key.encode()).hexdigest()
