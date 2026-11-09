# src/presentation/gateway_security.py
"""Короткое доказательство доверенного SSH-входа; Host localhost не повышает полномочия."""

from __future__ import annotations

from hashlib import sha256

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.presentation.security import COOKIE, read_session

GATEWAY_HEADER = "X-Mapper-Operator-Gateway"
PROOF_PATHS = {
    ("GET", "/api/v1/operator/session"),
    ("POST", "/api/v1/operator/tickets"),
}


def proof_serializer(settings) -> URLSafeTimedSerializer:
    """Отделяет подпись межсерверного доказательства от cookie и одноразовых кодов."""
    return URLSafeTimedSerializer(
        settings.session_secret.get_secret_value(), salt="mapper-ssh-gateway-v1"
    )


def proof_payload(method: str, path: str, cookie: str, origin: str, host: str) -> list[str]:
    """Связывает доказательство с конкретным запросом и cookie без раскрытия их значений."""
    return [method, path, sha256(cookie.encode()).hexdigest(), origin, host]


def issue_gateway_proof(
    settings, method: str, path: str, cookie: str, origin: str, host: str
) -> str:
    """Подписывает ограниченный запрос только внутри loopback gateway, не в браузере."""
    return proof_serializer(settings).dumps(proof_payload(method, path, cookie, origin, host))


def has_gateway_proof(request: Request) -> bool:
    """Проверяет 15-секундный proof и обычную cookie; публичный localhost Host недостаточен."""
    if (request.method, request.url.path) not in PROOF_PATHS or read_session(request) is None:
        return False
    settings = request.app.state.container.settings
    try:
        payload = proof_serializer(settings).loads(
            request.headers.get(GATEWAY_HEADER, ""), max_age=15
        )
    except BadSignature:
        return False
    return payload == proof_payload(
        request.method,
        request.url.path,
        request.cookies.get(COOKIE, ""),
        request.headers.get("origin", ""),
        request.headers.get("host", ""),
    )
