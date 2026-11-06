# tests/test_bootstrap_security.py
"""Безопасность bootstrap: секреты атомарны, hash совместим, credentials не попадают в argv."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from src.config import Settings
from src.domain.models import MapperError
from src.infrastructure.rabbitmq_bootstrap import (
    password_hash,
    provision_project_broker,
    verify_password,
)
from src.infrastructure.secrets import prepare_project_secrets


def test_hash_matches_existing_password_without_modifying_it():
    """Проверяет официальную salt/hash схему RabbitMQ и отказ при другом пароле."""
    password = SecretStr("secret-project-password")
    hashed = password_hash(password)
    user = {"password_hash": hashed, "hashing_algorithm": "rabbit_password_hashing_sha256"}
    assert verify_password(password, user)
    assert not verify_password(SecretStr("wrong"), user)
    assert "secret-project-password" not in hashed


def test_secrets_atomic_failure_and_named_key_generation(tmp_path, monkeypatch):
    """Неудачный replace оставляет старый .env дословно; повтор сохраняет все созданные ключи."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".env"
    original = "API_KEY=" + "x" * 32 + "\n"
    path.write_text(original, encoding="utf-8")
    replace = os.replace

    def fail(*args):
        """Моделирует отказ atomic rename до замены исходного файла."""
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        prepare_project_secrets()
    assert path.read_text(encoding="utf-8") == original
    monkeypatch.setattr(os, "replace", replace)
    prepare_project_secrets()
    first = path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    settings = Settings()
    assert set(settings.api_keys) == {"onec", "telegram"}
    assert settings.api_key.get_secret_value() == "x" * 32
    assert not settings.extracting_api_key.get_secret_value()
    prepare_project_secrets()
    assert path.read_text(encoding="utf-8") == first


def test_management_bootstrap_transmits_hash_and_never_plaintext_argv(settings):
    """Создаёт project namespace через HTTP без plaintext пароля в Docker-командах."""
    calls, requests = [], []
    info = {
        "Config": {"Env": ["RABBITMQ_DEFAULT_USER=admin", "RABBITMQ_DEFAULT_PASS=private-admin"]},
        "NetworkSettings": {"Networks": {"ai-shared": {"IPAddress": "172.18.0.2"}}},
    }

    def docker(*args):
        """Возвращает минимальную Docker discovery информацию без печати credentials."""
        calls.append(args)
        return SimpleNamespace(stdout="id\n" if args[0] == "ps" else json.dumps([info]))

    def handler(request):
        """Эмулирует создание новых user/vhost и проверяет безопасный HTTP payload."""
        requests.append(request)
        return httpx.Response(404 if request.method == "GET" else 204)

    def factory(**kwargs):
        """Подменяет только HTTP transport для unit проверки protocol Management API."""
        return httpx.Client(**kwargs, transport=httpx.MockTransport(handler))

    provision_project_broker(settings, docker, client_factory=factory)
    assert all(call[0] in {"ps", "inspect"} for call in calls)
    user_request = next(
        request for request in requests if request.method == "PUT" and "/users/" in request.url.path
    )
    body = json.loads(user_request.content)
    assert "password" not in body
    assert verify_password(settings.rabbitmq_password, body)
    assert settings.rabbitmq_password.get_secret_value() not in str(calls)


def test_existing_wrong_password_does_not_create_or_modify_any_resource(settings):
    """Неверный существующий пароль приводит к предметной ошибке до любого PUT/изменения прав."""
    info = {
        "Config": {"Env": ["RABBITMQ_DEFAULT_USER=admin", "RABBITMQ_DEFAULT_PASS=admin-private"]},
        "NetworkSettings": {"Networks": {"ai-shared": {"IPAddress": "172.18.0.2"}}},
    }
    methods = []

    def docker(*args):
        """Возвращает один действующий broker для теста отказа существующего credential."""
        return SimpleNamespace(stdout="id\n" if args[0] == "ps" else json.dumps([info]))

    def handler(request):
        """Отдаёт hash чужого пароля; любое изменение было бы регрессией безопасности."""
        methods.append(request.method)
        return httpx.Response(
            200, json={"tags": [], "password_hash": password_hash(SecretStr("other"))}
        )

    def factory(**kwargs):
        """Изолирует Management API от настоящего shared RabbitMQ."""
        return httpx.Client(**kwargs, transport=httpx.MockTransport(handler))

    with pytest.raises(MapperError) as error:
        provision_project_broker(settings, docker, client_factory=factory)
    assert error.value.code == "rabbit_password_mismatch"
    assert methods == ["GET"]
