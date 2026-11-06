# src/infrastructure/secrets.py
"""Атомарная подготовка sparse .env; существующие credentials сохраняются под межпроцессным lock."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

from src.config import Settings


@contextmanager
def secrets_lock():
    """Сериализует генераторы на Windows/Linux; завершение процесса автоматически снимает lock."""
    Path(".cache").mkdir(exist_ok=True)
    with Path(".cache/secrets.lock").open("a+b") as stream:
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prepare_project_secrets() -> None:
    """Добавляет отсутствующие секреты атомарным replace с 0600; upstream ключ не придумывает."""
    with secrets_lock():
        settings = Settings()
        additions = []
        for name in (
            "database_password",
            "rabbitmq_password",
            "session_secret",
            "catalog_admin_key",
        ):
            if not getattr(settings, name).get_secret_value():
                additions.append(f"{name.upper()}={secrets.token_urlsafe(36)}")
        keys = {name: value.get_secret_value() for name, value in settings.api_keys.items()}
        changed = False
        for owner in ("onec", "telegram"):
            if owner not in keys:
                keys[owner] = secrets.token_urlsafe(36)
                changed = True
        if changed:
            additions.append(
                "API_KEYS=" + json.dumps(keys, ensure_ascii=True, separators=(",", ":"))
            )
        path = Path(".env")
        if path.is_symlink():
            raise ValueError(".env не должен быть символической ссылкой")
        if additions:
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            descriptor, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=".")
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(original.rstrip("\n") + "\n" + "\n".join(additions) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
        if path.exists():
            os.chmod(path, 0o600)
        Settings().require_secrets()
