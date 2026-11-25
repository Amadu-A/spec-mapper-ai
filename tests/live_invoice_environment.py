# tests/live_invoice_environment.py
"""Linux-only управление временным live QA; секреты остаются на Docker-хосте.

Копия кода создаётся с Windows. Этот модуль не правит production checkout и shared
сервисы: создаёт только свои API/worker контейнеры, SQL schema и project queue.
Контейнеры используют настоящий каталог через read-only адаптер live_invoice_runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def command(*arguments: str) -> str:
    """Забирает stdout в память; конфигурация/секреты никогда не печатаются при отказе."""
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("QA command failed: " + arguments[0])
    return result.stdout


def root_info(value: str) -> tuple[Path, str]:
    """Разрешает только собственный каталог /tmp с уникальным hex namespace."""
    root = Path(value)
    if (
        root.parent != Path("/tmp")
        or root.is_symlink()
        or not re.fullmatch(r"spec-mapper-invoice-qa-[a-f0-9]{16}", root.name)
    ):
        raise ValueError("Некорректный каталог QA")
    return root, root.name.removeprefix("spec-mapper-invoice-qa-")


def state(root: Path) -> dict:
    """Читает только несекретные идентификаторы ресурсов данного прогона."""
    return json.loads((root / "state.json").read_text())


def prepare(root: Path, suffix: str) -> None:
    """Читает необходимые Settings старого API в память и пишет private.env вне build context."""
    extraction = """
import json
from pathlib import Path
from pydantic import SecretStr
from src.config import Settings
settings=Settings()
names=['database_host','database_port','database_name','database_user','database_password',
       'rabbitmq_host','rabbitmq_port','rabbitmq_user','rabbitmq_vhost','rabbitmq_password',
       'shared_embedding_base_url','shared_embedding_model','embedding_revision','embedding_timeout_seconds',
       'qdrant_url','qdrant_alias','extracting_base_url','extracting_api_key',
       'api_key','api_keys','catalog_admin_key','session_secret']
def encode(value):
    if isinstance(value,SecretStr): return value.get_secret_value()
    if isinstance(value,dict): return {key:encode(item) for key,item in value.items()}
    if isinstance(value,Path): return str(value)
    return value
print(json.dumps({name.upper():encode(getattr(settings,name)) for name in names}))
"""
    environment = json.loads(
        command("docker", "exec", "spec-mapper-ai-api-1", "/app/.venv/bin/python", "-c", extraction)
    )
    api = "spec-mapper-invoice-qa-api-" + suffix
    worker = "spec-mapper-invoice-qa-worker-" + suffix
    inspected = json.loads(command("docker", "inspect", "spec-mapper-ai-api-1"))[0]
    networks = inspected["NetworkSettings"]["Networks"]
    private = []
    for network in networks:
        metadata = json.loads(command("docker", "network", "inspect", network))[0]
        if metadata.get("Labels", {}).get("com.docker.compose.network") == "private":
            private.append(network)
    if len(private) != 1 or "ai-shared" not in networks:
        raise ValueError("Не подтверждены сети Mapper")
    environment.update(
        QA_SCHEMA="mapper_invoice_qa_" + suffix,
        RABBITMQ_QUEUE="spec_mapper.invoice_qa." + suffix,
        CATALOG_DAILY_SYNC="false",
        RATE_LIMIT_PER_MINUTE=10000,
        APP_PUBLIC_BASE_URL="http://127.0.0.1:8087",
        PYTHONPATH="/app:/app/tests",
        PYTHONUNBUFFERED="1",
    )
    lines = []
    for name, value in environment.items():
        text = (
            json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        )
        if "\n" in text or "\r" in text:
            raise ValueError("Многострочная конфигурация не поддерживается QA")
        lines.append(name + "=" + text)
    path = root / "private.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    metadata = {
        "suffix": suffix,
        "api": api,
        "worker": worker,
        "private_network": private[0],
        "image": "spec-mapper-ai-invoice-qa:" + suffix,
        "schema": environment["QA_SCHEMA"],
        "queue": environment["RABBITMQ_QUEUE"],
        "uid": os.getuid(),
        "gid": os.getgid(),
    }
    (root / "state.json").write_text(json.dumps(metadata), encoding="utf-8")
    print(json.dumps({"qa_prepared": True, "schema": metadata["schema"]}), flush=True)


def create(root: Path, metadata: dict, name: str, action: str, with_corpus: bool = False) -> None:
    """Создаёт свой QA контейнер в network namespace API без подключения сетей/публикации портов."""
    arguments = [
        "docker",
        "create",
        "--init",
        "--user",
        str(metadata["uid"]) + ":" + str(metadata["gid"]),
        "--name",
        name,
        "--label",
        "spec_mapper_invoice_qa=" + metadata["suffix"],
        "--network",
        "container:spec-mapper-ai-api-1",
        "--env-file",
        str(root / "private.env"),
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
    ]
    if with_corpus:
        arguments += [
            "--mount",
            "type=bind,src=" + str(root / "corpus") + ",dst=/app/examples/invoices,readonly",
        ]
    arguments += [
        metadata["image"],
        "/app/.venv/bin/python",
        "-m",
        "tests.live_invoice_runtime",
        action,
    ]
    command(*arguments)


def run_control(root: Path, metadata: dict, action: str) -> None:
    """Запускает одноразовую миграцию/очистку только изолированных SQL/queue ресурсов."""
    name = "spec-mapper-invoice-qa-control-" + metadata["suffix"]
    create(root, metadata, name, action)
    try:
        output = command("docker", "start", "--attach", name)
        code = json.loads(command("docker", "inspect", name))[0]["State"]["ExitCode"]
        if code:
            raise RuntimeError("QA control failed")
        print(output.strip(), flush=True)
    finally:
        remove_container(name, metadata["suffix"])


def remove_container(name: str, suffix: str) -> None:
    """Удаляет только контейнер с точным собственным label; чужое имя не принимается."""
    result = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return
    inspected = json.loads(result.stdout)[0]
    if inspected["Config"]["Labels"].get("spec_mapper_invoice_qa") != suffix:
        raise ValueError("Контейнер не принадлежит этому QA")
    command("docker", "rm", "--force", name)


def main() -> None:
    """Управляет явно выбранным QA-действием; никаких commit/push или production recreate."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "migrate",
            "start",
            "recreate",
            "recreate-api",
            "status",
            "stop-worker",
            "cleanup",
        ],
    )
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root, suffix = root_info(args.root)
    if args.action == "prepare":
        prepare(root, suffix)
        return
    metadata = state(root)
    if metadata["suffix"] != suffix:
        raise ValueError("QA state namespace mismatch")
    if args.action == "migrate":
        run_control(root, metadata, "migrate")
    elif args.action == "recreate-api":
        remove_container(metadata["api"], suffix)
        create(root, metadata, metadata["api"], "api", with_corpus=True)
        command("docker", "start", metadata["api"])
        print(json.dumps({"qa_api_recreated": True, "worker_unchanged": True}), flush=True)
    elif args.action in {"start", "recreate"}:
        if args.action == "recreate":
            for name in [metadata["api"], metadata["worker"]]:
                remove_container(name, suffix)
        create(root, metadata, metadata["worker"], "worker")
        create(root, metadata, metadata["api"], "api", with_corpus=True)
        command("docker", "start", metadata["worker"], metadata["api"])
        print(json.dumps({"qa_started": True, "api_port": 8087}), flush=True)
    elif args.action == "status":
        for name in [metadata["api"], metadata["worker"]]:
            inspected = json.loads(command("docker", "inspect", name))[0]
            print(
                json.dumps(
                    {
                        "name": name,
                        "state": inspected["State"]["Status"],
                        "ports": inspected["NetworkSettings"]["Ports"],
                    }
                ),
                flush=True,
            )
    elif args.action == "stop-worker":
        inspected = json.loads(command("docker", "inspect", metadata["worker"]))[0]
        if inspected["Config"]["Labels"].get("spec_mapper_invoice_qa") != suffix:
            raise ValueError("Worker не принадлежит этому QA")
        command("docker", "stop", metadata["worker"])
        print(json.dumps({"qa_worker_stopped": True}), flush=True)
    elif args.action == "cleanup":
        for name in [metadata["api"], metadata["worker"]]:
            remove_container(name, suffix)
        run_control(root, metadata, "cleanup")
        inspected = json.loads(command("docker", "image", "inspect", metadata["image"]))[0]
        if inspected["Config"].get("Labels", {}).get("spec_mapper_invoice_qa") == suffix:
            command("docker", "image", "rm", metadata["image"])
        if (
            root.resolve().parent != Path("/tmp")
            or root.is_symlink()
            or root.stat().st_uid != os.getuid()
        ):
            raise ValueError("Небезопасная очистка QA каталога")
        shutil.rmtree(root)
        print(json.dumps({"qa_cleanup_complete": True}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"qa_environment_error": type(error).__name__}), flush=True)
        sys.exit(1)
