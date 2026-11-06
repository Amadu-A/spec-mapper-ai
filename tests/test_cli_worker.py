# tests/test_cli_worker.py
"""Проверки операторских секретов, outbox publish confirm и восстановления после сбоя dispatcher."""

import asyncio
from pathlib import Path

import pytest

from src.cli import prepare_secrets
from src.worker import maintenance


def test_sparse_secrets_are_idempotent_and_existing_password_is_preserved(tmp_path, monkeypatch):
    """Bootstrap записывает только секреты и никогда не меняет уже установленный пароль."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("APP_PORT=8084\n", encoding="utf-8")
    password = "existing-project-password" * 2
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=" + password + "\n", encoding="utf-8")
    prepare_secrets()
    first = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DATABASE_PASSWORD=" + password in first
    assert "APP_PORT" not in first
    assert len([line for line in first.splitlines() if "=" in line]) == 5
    prepare_secrets()
    assert Path(".env").read_text(encoding="utf-8") == first


async def test_dispatcher_marks_only_confirmed_messages(system, source, monkeypatch):
    """Сбой publisher оставляет задание в outbox; успешный confirm фиксируется отдельно."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "dispatch", source)
    published = []

    async def publish(job_id):
        """Подменяет только broker publish успешным publisher confirm."""
        published.append(job_id)

    async def stop(delay):
        """Останавливает maintenance после одного цикла без реального ожидания."""
        raise asyncio.CancelledError

    monkeypatch.setattr(container.broker, "publish", publish)
    monkeypatch.setattr("src.worker.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await maintenance(container)
    assert published == [job["id"]]
    assert job["id"] not in await container.jobs.pending()


async def test_dispatcher_failure_does_not_lose_outbox(system, source, monkeypatch):
    """Публикация, не получившая confirm, остаётся доступной для повторного dispatcher цикла."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "dispatch-fail", source)

    async def fail(job_id):
        """Эмулирует потерю связи до publisher confirm без изменения durable DB."""
        raise ConnectionError("no broker")

    async def stop(delay):
        """Останавливает maintenance после обработанного сбоя одной публикации."""
        raise asyncio.CancelledError

    monkeypatch.setattr(container.broker, "publish", fail)
    monkeypatch.setattr("src.worker.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await maintenance(container)
    assert job["id"] in await container.jobs.pending()


async def test_scheduler_configuration_failure_does_not_block_mapping(system, source, monkeypatch):
    """Ошибка источника расписания не задерживает доставку независимых mapping jobs."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "schedule-failure", source)
    published = []

    async def tick():
        """Имитирует отказ конфигурации до enqueue импорта."""
        raise ValueError("invalid source configuration")

    async def publish(job_id):
        """Подтверждает доставку независимого mapping UUID."""
        published.append(job_id)

    async def stop(delay):
        """Завершает проверку после одного цикла maintenance."""
        raise asyncio.CancelledError

    monkeypatch.setattr(container.sync, "tick", tick)
    monkeypatch.setattr(container.broker, "publish", publish)
    monkeypatch.setattr("src.worker.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await maintenance(container)
    assert published == [job["id"]]
