# tests/test_settings_logging.py
"""Приоритет Pydantic/Compose и timing для sync/async success, errors и cancellation."""

import asyncio
import logging

import pytest

from src.config import Settings
from src.observability import timed


def test_settings_priority_and_new_defaults(tmp_path, monkeypatch):
    """Process environment перекрывает .env, а новый обычный параметр читается из baseline."""
    baseline, private = tmp_path / ".env.example", tmp_path / ".env"
    baseline.write_text("APP_PORT=8084\nTOP_K=7\n", encoding="utf-8")
    private.write_text("APP_PORT=8085\n", encoding="utf-8")
    assert Settings(_env_file=(baseline, private)).app_port == 8085
    assert Settings(_env_file=(baseline, private)).top_k == 7
    monkeypatch.setenv("APP_PORT", "8086")
    assert Settings(_env_file=(baseline, private)).app_port == 8086


def test_all_settings_are_catalogued_and_forwarded():
    """Новая переменная не теряется между Python baseline и environment override Compose."""
    from pathlib import Path

    baseline = Path(".env.example").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    for name in Settings.model_fields:
        assert name.upper() + "=" in baseline, name
        assert name.upper() + ":" in compose, name


def test_named_keys_validate_identity_and_null_json():
    """Исключает неоднозначный owner и сохраняет пустую конфигурацию до bootstrap."""
    assert Settings(_env_file=None, api_keys=None).api_keys == {}
    with pytest.raises(ValueError):
        Settings(_env_file=None, api_keys={"onec": "same", "telegram": "same"})
    with pytest.raises(ValueError):
        Settings(_env_file=None, api_key="same", api_keys={"onec": "same"})


def test_configuration_error_does_not_print_secret_values():
    """Диагностика невалидного API_KEYS не раскрывает значения даже в startup traceback."""
    secret = "private-value-that-must-never-be-logged"
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, api_keys={"onec": secret, "telegram": secret})
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "asynchronous,fail", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_timing_metadata_duration_and_status(caplog, asynchronous, fail):
    """Фиксирует одну итоговую запись с длительностью и сохраняет имя исходной функции."""

    def synchronous():
        """Создаёт контролируемый sync success/error для проверки timing контракта."""
        if fail:
            raise ValueError("expected")
        return 1

    async def coroutine():
        """Создаёт контролируемый async success/error без I/O."""
        return synchronous()

    original = coroutine if asynchronous else synchronous
    wrapped = timed("test_operation")(original)
    assert wrapped.__name__ == original.__name__
    caplog.set_level(logging.INFO)
    try:
        result = await wrapped() if asynchronous else wrapped()
        assert result == 1
    except ValueError:
        assert fail
    records = [record for record in caplog.records if record.msg == "operation_timing"]
    assert len(records) == 1
    assert records[0].duration_ms >= 0
    assert records[0].status == ("error" if fail else "success")


async def test_async_cancel_keeps_cancelled_error(caplog):
    """Отмена операции не превращается в success или обычный результат."""

    @timed("cancel_test")
    async def cancelled():
        """Моделирует отмену HTTP/worker pipeline без ожидания реального timeout."""
        raise asyncio.CancelledError

    caplog.set_level(logging.INFO)
    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert caplog.records[-1].status == "error"
