# src/cli.py
"""Операторские команды: безопасный bootstrap, ранняя диагностика и ожидание durable импорта."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import subprocess
from time import time

import httpx
from pydantic import ValidationError

from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import MapperError, digest
from src.infrastructure.rabbitmq_bootstrap import discover_broker, provision_project_broker
from src.infrastructure.secrets import prepare_project_secrets
from src.infrastructure.xml_catalog import XmlCatalogSource


def prepare_secrets() -> None:
    """Атомарно готовит отсутствующие project secrets, сохраняя старый API_KEY и пароли."""
    prepare_project_secrets()
    print(
        "Проектные секреты подготовлены; существующие значения сохранены. "
        "Upstream ключ не создавался."
    )


def docker(*arguments: str, input_text: str | None = None, check: bool = True):
    """Запускает Docker без shell и скрытого вывода credentials из inspect/diagnostics."""
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
            "Docker недоступен или отклонил проверку; проверьте права и daemon",
            503,
        )
    return result


def runtime_preflight() -> None:
    """Проверяет Compose, сеть, host port, реальный XML и shared endpoint до записи секретов."""
    settings = Settings()
    docker("info")
    version = docker("compose", "version", "--short").stdout.strip()
    numbers = re.search(r"(\d+)\.(\d+)", version)
    if numbers is None or tuple(map(int, numbers.groups())) < (2, 24):
        raise MapperError("compose_version", "Требуется Docker Compose 2.24 или новее")
    docker("network", "inspect", "ai-shared")
    with socket.socket() as connection:
        try:
            connection.bind((settings.app_bind, settings.app_port))
        except OSError:
            ids = docker(
                "ps",
                "--filter",
                "label=com.docker.compose.project=spec-mapper-ai",
                "--filter",
                "label=com.docker.compose.service=api",
                "--format",
                "{{.ID}}",
            ).stdout.split()
            if len(ids) != 1:
                raise MapperError("port_busy", "Порт web/API занят другим процессом") from None
            info = json.loads(docker("inspect", ids[0]).stdout)[0]
            bindings = info["NetworkSettings"]["Ports"].get("8000/tcp") or []
            if not any(int(binding["HostPort"]) == settings.app_port for binding in bindings):
                raise MapperError("port_busy", "Выбранный порт занят чужим приложением") from None
    parsed = XmlCatalogSource(
        settings.data_directory, settings.max_xml_bytes, settings.duplicate_code_policy
    )._read()
    discover_broker(docker)
    ids = docker(
        "ps", "--filter", "label=com.docker.compose.service=shared-embedding", "--format", "{{.ID}}"
    ).stdout.split()
    addresses = []
    for container_id in ids:
        info = json.loads(docker("inspect", container_id).stdout)[0]
        network = info["NetworkSettings"]["Networks"].get("ai-shared")
        if network and network.get("IPAddress"):
            addresses.append(network["IPAddress"])
    if len(addresses) != 1:
        raise MapperError(
            "embedding_discovery", "Нужен работающий shared-embedding в ai-shared", 503
        )
    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.get(f"http://{addresses[0]}:8000/v1/models")
            response.raise_for_status()
            if settings.shared_embedding_model not in {m["id"] for m in response.json()["data"]}:
                raise ValueError("Неверный alias")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise MapperError(
            "embedding_unavailable", "Shared embedding не подтвердил logical model alias", 503
        ) from exc
    print(
        f"Preflight: порт {settings.app_port} свободен или принадлежит проекту; "
        f"XML: {len(parsed.items)} позиций."
    )


def provision_rabbitmq() -> None:
    """Создаёт только project user/vhost через Management API с password_hash вместо argv пароля."""
    settings = Settings()
    settings.require_secrets()
    provision_project_broker(settings, docker)
    print(
        "Выделенные user/vhost spec_mapper проверены; чужие credentials и lifecycle не изменялись."
    )


def check_live_stack() -> None:
    """Отклоняет live tests до запуска project stack вместо каскада DNS/auth ошибок."""
    settings = Settings()
    settings.require_secrets()
    result = docker(
        "compose", "--env-file", ".env.example", "--env-file", ".env", "ps", "--format", "json"
    ).stdout.strip()
    rows = (
        json.loads(result)
        if result.startswith("[")
        else [json.loads(line) for line in result.splitlines() if line]
    )
    running = {row["Service"] for row in rows if row.get("State") == "running"}
    missing = {"postgres", "qdrant", "api", "worker"} - running
    if missing:
        raise MapperError(
            "stack_not_running",
            "Не запущены: "
            + ", ".join(sorted(missing))
            + ". Сначала выполните bash scripts/up.sh с реальным XML",
            503,
        )
    try:
        response = httpx.get(
            settings.app_public_base_url.rstrip("/") + "/health/ready", timeout=20, trust_env=False
        )
        response.raise_for_status()
        if not response.json().get("ready"):
            raise ValueError("Not ready")
    except (httpx.HTTPError, ValueError) as exc:
        raise MapperError(
            "stack_not_ready", "Project stack ещё не ready; проверьте журналы api/worker", 503
        ) from exc


async def shared_health() -> None:
    """До старта worker проверяет реальные embedding и RabbitMQ из сети ai-shared."""
    container = build_container(Settings())
    try:
        await container.embeddings.identity()
        await container.broker.connect()
        print("Shared embedding и проектная авторизация RabbitMQ подтверждены.")
    finally:
        await container.close()


async def enqueue_catalog(wait: bool = False) -> None:
    """Ожидает административный импорт по его отдельному deadline, показывая безопасные counts."""
    container = build_container(Settings())
    try:
        identity = await container.embeddings.identity()
        source = await XmlCatalogSource(
            container.settings.data_directory,
            container.settings.max_xml_bytes,
            container.settings.duplicate_code_policy,
        ).read()
        status = await container.catalog.status()
        await container.jobs.cleanup()
        key = "startup:" + digest(
            [
                source.source_hash,
                identity.get("metadata_fingerprint", identity["fingerprint"]),
                status.get("last_successful_sync"),
            ]
        )
        job = await container.jobs.create("system", key, {}, "catalog")
        while job["state"] == "failed":
            key = "retry:" + digest([key, job["id"]])
            job = await container.jobs.create("system", key, {}, "catalog")
        await container.broker.publish(job["id"])
        await container.jobs.dispatched(job["id"])
        print("Задание импорта: " + job["id"])
        last_progress = None
        if wait:
            while time() < job["expires_at"]:
                current = await container.jobs.get(job["id"], "system")
                progress = current["data"].get("catalog_progress", {})
                bucket = (current["stage"], progress.get("processed", 0) // 1000)
                if bucket != last_progress:
                    print(
                        f"{current['stage']}: "
                        f"{progress.get('processed', 0)}/{progress.get('total', 0)}",
                        flush=True,
                    )
                    last_progress = bucket
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
            raise MapperError(
                "catalog_import_timeout", "Истекло отдельное время ожидания каталога", 503
            )
    finally:
        await container.close()


def main() -> None:
    """Выполняет одну операторскую операцию и выдаёт только безопасную предметную диагностику."""
    parser = argparse.ArgumentParser(description="Операции spec-mapper-ai")
    parser.add_argument(
        "command",
        choices=[
            "secrets",
            "preflight",
            "rabbitmq",
            "import-catalog",
            "check-live",
            "shared-health",
        ],
    )
    parser.add_argument("--wait", action="store_true", help="Дождаться завершения импорта")
    args = parser.parse_args()
    try:
        if args.command == "secrets":
            prepare_secrets()
        elif args.command == "preflight":
            runtime_preflight()
        elif args.command == "rabbitmq":
            provision_rabbitmq()
        elif args.command == "check-live":
            check_live_stack()
        elif args.command == "shared-health":
            asyncio.run(shared_health())
        else:
            asyncio.run(enqueue_catalog(args.wait))
    except MapperError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from None
    except (ValidationError, ValueError):
        raise SystemExit(
            "configuration_invalid: проверьте имена, типы и длину секретов в конфигурации"
        ) from None


if __name__ == "__main__":
    main()
