# src/infrastructure/rabbitmq_bootstrap.py
"""Узкий bootstrap project user/vhost через Management API без plaintext пароля в argv и логах."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import quote

import httpx
from pydantic import BaseModel, SecretStr

from src.domain.models import MapperError


class BootstrapCredentials(BaseModel):
    """Временные credentials текущего RabbitMQ; в проектный .env не сохраняются."""

    username: str
    password: SecretStr


def password_hash(password: SecretStr) -> str:
    """Вычисляет RabbitMQ SHA256 hash с криптографической четырёхбайтной солью."""
    salt = secrets.token_bytes(4)
    hashed = hashlib.sha256(salt + password.get_secret_value().encode()).digest()
    return base64.b64encode(salt + hashed).decode("ascii")


def verify_password(password: SecretStr, user: dict) -> bool:
    """Проверяет существующий hash локально, не меняя credentials и не помещая пароль в процессы."""
    algorithms = {
        "rabbit_password_hashing_sha256": hashlib.sha256,
        "rabbit_password_hashing_sha512": hashlib.sha512,
    }
    algorithm = algorithms.get(user.get("hashing_algorithm", "rabbit_password_hashing_sha256"))
    if algorithm is None:
        raise MapperError(
            "rabbit_hash_algorithm", "Алгоритм пароля существующего user не поддерживается"
        )
    try:
        stored = base64.b64decode(user.get("password_hash", ""), validate=True)
    except ValueError:
        return False
    return len(stored) > 4 and hmac.compare_digest(
        stored[4:], algorithm(stored[:4] + password.get_secret_value().encode()).digest()
    )


def discover_broker(docker) -> dict:
    """Находит ровно один действующий shared RabbitMQ в ai-shared без вывода Config.Env."""
    ids = docker(
        "ps", "--filter", "label=com.docker.compose.service=rabbitmq", "--format", "{{.ID}}"
    ).stdout.split()
    found = []
    for container_id in ids:
        info = json.loads(docker("inspect", container_id).stdout)[0]
        if "ai-shared" in info["NetworkSettings"]["Networks"]:
            found.append(info)
    if len(found) != 1:
        raise MapperError(
            "rabbit_discovery", "Нужен один работающий shared RabbitMQ в ai-shared", 503
        )
    return found[0]


def provision_project_broker(settings, docker, client_factory=httpx.Client) -> None:
    """Создаёт только выделенный namespace и подтверждает существующий пароль без его замены."""
    namespace = settings.rabbitmq_user
    if namespace != settings.rabbitmq_vhost or not re.fullmatch(
        r"spec_mapper(?:_test_[a-f0-9]{12})?", namespace
    ):
        raise MapperError(
            "rabbit_namespace", "Разрешён только namespace spec_mapper или изолированный тестовый"
        )
    info = discover_broker(docker)
    environment = dict(
        value.split("=", 1) for value in info["Config"].get("Env", []) if "=" in value
    )
    credentials = BootstrapCredentials(
        username=environment.get("RABBITMQ_DEFAULT_USER", ""),
        password=environment.get("RABBITMQ_DEFAULT_PASS", ""),
    )
    if not credentials.username or not credentials.password.get_secret_value():
        raise MapperError(
            "rabbit_admin_missing",
            "Bootstrap admin credentials отсутствуют в deployment RabbitMQ",
            503,
        )
    address = info["NetworkSettings"]["Networks"]["ai-shared"].get("IPAddress")
    if not address:
        raise MapperError(
            "rabbit_management_address", "У RabbitMQ нет IPv4 адреса в ai-shared", 503
        )
    user_path, vhost_path = quote(namespace, safe=""), quote(settings.rabbitmq_vhost, safe="")
    with client_factory(
        base_url=f"http://{address}:15672",
        timeout=15,
        trust_env=False,
        auth=httpx.BasicAuth(credentials.username, credentials.password.get_secret_value()),
    ) as client:

        def request(method: str, path: str, **kwargs):
            """Не раскрывает response body, credentials или служебный HTTP auth при ошибке."""
            try:
                response = client.request(method, "/api" + path, **kwargs)
                if response.status_code == 404 and method == "GET":
                    return None
                response.raise_for_status()
                return response.json() if response.content else None
            except (httpx.HTTPError, ValueError) as exc:
                raise MapperError(
                    "rabbit_management",
                    "Management API RabbitMQ недоступен или отклонил bootstrap",
                    503,
                ) from exc

        user = request("GET", "/users/" + user_path)
        if user is not None:
            if user.get("tags") not in ([], "", None):
                raise MapperError("rabbit_ownership", "User имеет административную или чужую роль")
            if not verify_password(settings.rabbitmq_password, user):
                raise MapperError(
                    "rabbit_password_mismatch",
                    "Пароль spec_mapper не совпадает; существующий пароль не изменён",
                )
            permissions = request("GET", "/users/" + user_path + "/permissions") or []
            if any(permission["vhost"] != settings.rabbitmq_vhost for permission in permissions):
                raise MapperError("rabbit_ownership", "User связан с другим vhost")
        vhost = request("GET", "/vhosts/" + vhost_path)
        if vhost is not None:
            permissions = request("GET", "/vhosts/" + vhost_path + "/permissions") or []
            users = request("GET", "/users") or []
            admins = {entry["name"] for entry in users if "administrator" in entry.get("tags", [])}
            if any(permission["user"] not in admins | {namespace} for permission in permissions):
                raise MapperError("rabbit_ownership", "Vhost используется другим приложением")
        if user is None:
            request(
                "PUT",
                "/users/" + user_path,
                json={
                    "password_hash": password_hash(settings.rabbitmq_password),
                    "hashing_algorithm": "rabbit_password_hashing_sha256",
                    "tags": "",
                },
            )
        if vhost is None:
            request("PUT", "/vhosts/" + vhost_path, json={})
        request(
            "PUT",
            "/permissions/" + vhost_path + "/" + user_path,
            json={"configure": ".*", "write": ".*", "read": ".*"},
        )
