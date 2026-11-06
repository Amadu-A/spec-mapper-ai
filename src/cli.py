# src/cli.py
"""Операторские операции: sparse secrets, проверка host, изолированный RabbitMQ user и импорт."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
from pathlib import Path
from time import time

from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import MapperError


def prepare_secrets() -> None:
    """Добавляет только отсутствующие проектные секреты, не меняя существующие credentials."""
    settings = Settings()
    additions = []
    for name in (
        "database_password",
        "rabbitmq_password",
        "session_secret",
        "api_key",
        "catalog_admin_key",
    ):
        if not getattr(settings, name).get_secret_value():
            additions.append(f"{name.upper()}={secrets.token_urlsafe(36)}\n")
    if additions:
        path = Path(".env")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write("\n" + "".join(additions))
    Settings().require_secrets()
    print("Проектные секреты подготовлены; существующие значения сохранены.")


def docker(*arguments: str, input_text: str | None = None, check: bool = True):
    """Запускает Docker без shell; stdout/stderr с возможными credentials не публикуются."""
    result = subprocess.run(
        ["docker", *arguments],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if check and result.returncode:
        raise MapperError(
            "docker_command",
            "Команда Docker/RabbitMQ не выполнена; проверьте состояние сервера",
            503,
        )
    return result


def runtime_preflight() -> None:
    """Проверяет существующую ai-shared и порт, допуская только уже работающий собственный API."""
    settings = Settings()
    docker("info")
    docker("network", "inspect", "ai-shared")
    if settings.app_port == 8083:
        raise MapperError("port_reserved", "Порт 8083 зарезервирован для извлечения")
    with socket.socket() as connection:
        try:
            connection.bind((settings.app_bind, settings.app_port))
        except OSError:
            result = docker(
                "ps",
                "--filter",
                "label=com.docker.compose.project=spec-mapper-ai",
                "--filter",
                "label=com.docker.compose.service=api",
                "--format",
                "{{.ID}}",
            )
            ids = result.stdout.split()
            if len(ids) != 1:
                raise MapperError("port_busy", "Порт web/API занят другим процессом") from None
            info = json.loads(docker("inspect", ids[0]).stdout)[0]
            bindings = info["NetworkSettings"]["Ports"].get("8000/tcp") or []
            if not any(int(b["HostPort"]) == settings.app_port for b in bindings):
                raise MapperError("port_busy", "Выбранный порт занят чужим приложением") from None
    print(f"ai-shared доступна; проектный web-порт: {settings.app_port}")


def provision_rabbitmq() -> None:
    """Создаёт только project user/vhost; не сбрасывает пароль и не меняет чужие permissions."""
    settings = Settings()
    settings.require_secrets()
    if settings.rabbitmq_user != "spec_mapper" or settings.rabbitmq_vhost != "spec_mapper":
        raise MapperError(
            "rabbit_namespace", "Автоподготовка разрешена только для namespace spec_mapper"
        )
    ids = docker(
        "ps", "--filter", "label=com.docker.compose.service=rabbitmq", "--format", "{{.ID}}"
    ).stdout.split()
    matching = []
    for container_id in ids:
        info = json.loads(docker("inspect", container_id).stdout)[0]
        if "ai-shared" in info["NetworkSettings"]["Networks"]:
            matching.append(container_id)
    if len(matching) != 1:
        raise MapperError(
            "rabbit_discovery", "Нужен один работающий shared RabbitMQ в ai-shared", 503
        )
    container_id = matching[0]

    def control(*arguments: str):
        """Вызывает rabbitmqctl только в обнаруженном broker и возвращает
        структурированный вывод.
        """
        return docker("exec", container_id, "rabbitmqctl", "-q", *arguments)

    users = json.loads(control("list_users", "--formatter=json").stdout)
    user = next((u for u in users if u["user"] == settings.rabbitmq_user), None)
    if user:
        if user.get("tags") not in ([], "[]", ""):
            raise MapperError(
                "rabbit_ownership", "Существующий пользователь имеет чужую административную роль"
            )
        permissions = json.loads(
            control("list_user_permissions", settings.rabbitmq_user, "--formatter=json").stdout
        )
        if any(p["vhost"] != settings.rabbitmq_vhost for p in permissions):
            raise MapperError("rabbit_ownership", "Пользователь уже связан с другим проектом")
    vhosts = json.loads(control("list_vhosts", "name", "--formatter=json").stdout)
    exists = any(v["name"] == settings.rabbitmq_vhost for v in vhosts)
    if exists:
        permissions = json.loads(
            control("list_permissions", "-p", settings.rabbitmq_vhost, "--formatter=json").stdout
        )
        admin_users = {u["user"] for u in users if "administrator" in str(u.get("tags", ""))}
        if any(p["user"] not in admin_users | {settings.rabbitmq_user} for p in permissions):
            raise MapperError("rabbit_ownership", "Vhost занят другим приложением")
    if not user:
        # Пароль передаётся только через stdin, не через host argv или вывод команды.
        docker(
            "exec",
            "-i",
            container_id,
            "sh",
            "-c",
            'read -r project_password; rabbitmqctl -q add_user spec_mapper "$project_password"',
            input_text=settings.rabbitmq_password.get_secret_value() + "\n",
        )
    else:
        docker(
            "exec",
            "-i",
            container_id,
            "sh",
            "-c",
            "read -r project_password; "
            'rabbitmqctl -q authenticate_user spec_mapper "$project_password"',
            input_text=settings.rabbitmq_password.get_secret_value() + "\n",
        )
    if not exists:
        control("add_vhost", settings.rabbitmq_vhost)
    control(
        "set_permissions", "-p", settings.rabbitmq_vhost, settings.rabbitmq_user, ".*", ".*", ".*"
    )
    print("Проверены выделенные user/vhost spec_mapper; shared контейнер не перезапускался.")


async def enqueue_catalog(wait: bool = False) -> None:
    """Публикует durable импорт из data; файлы и активный индекс проверяются в worker."""
    container = build_container(Settings())
    try:
        job = await container.jobs.create("system", f"startup:{int(time() // 30)}", {}, "catalog")
        await container.broker.publish(job["id"])
        await container.jobs.dispatched(job["id"])
        print("Запущено задание импорта каталога: " + job["id"])
        if wait:
            deadline = time() + container.settings.draft_ttl_seconds
            while time() < deadline:
                current = await container.jobs.get(job["id"], "system")
                if current["state"] == "complete":
                    status = await container.catalog.status()
                    print(f"Импорт завершён: {status['count']} позиций")
                    for warning in status.get("warnings", []):
                        print("Предупреждение: " + warning)
                    return
                if current["state"] == "failed":
                    raise MapperError(
                        "catalog_import_failed",
                        "Импорт не выполнен: " + str(current["error_code"]),
                        503,
                    )
                await asyncio.sleep(3)
            raise MapperError("catalog_import_timeout", "Истекло время ожидания импорта", 503)
    finally:
        await container.close()


def main() -> None:
    """Выбирает одну явно запрошенную операторскую операцию и печатает только безопасные ошибки."""
    parser = argparse.ArgumentParser(description="Операции spec-mapper-ai")
    parser.add_argument("command", choices=["secrets", "preflight", "rabbitmq", "import-catalog"])
    parser.add_argument("--wait", action="store_true", help="Дождаться завершения импорта каталога")
    args = parser.parse_args()
    try:
        if args.command == "secrets":
            prepare_secrets()
        elif args.command == "preflight":
            runtime_preflight()
        elif args.command == "rabbitmq":
            provision_rabbitmq()
        else:
            asyncio.run(enqueue_catalog(args.wait))
    except MapperError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from None


if __name__ == "__main__":
    main()
