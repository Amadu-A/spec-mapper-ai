# src/observability.py
"""Безопасное структурированное журналирование и единый timing для ключевых use-cases."""

from __future__ import annotations

import inspect
import json
import logging
from functools import wraps
from time import perf_counter


class JsonFormatter(logging.Formatter):
    """Разрешает только безопасные поля и исключает текст исключения с HTTP credentials."""

    def format(self, record: logging.LogRecord) -> str:
        """Формирует одну JSON-строку без произвольных payload и traceback внешних клиентов."""
        fields = {
            "level": record.levelname,
            "event": record.getMessage(),
            "timestamp": self.formatTime(record),
            "service": "spec-mapper",
        }
        for name in ("operation", "duration_ms", "status", "job_id", "error_code", "count"):
            if hasattr(record, name):
                fields[name] = getattr(record, name)
        return json.dumps(fields, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Настраивает stdout; ограничение размера и хранения обеспечивает Docker local driver."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "aio_pika", "aiormq", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.WARNING)


def timed(operation: str):
    """Измеряет sync/async операции монотонными часами; отмена остаётся ошибкой."""

    def decorate(function):
        """Сохраняет метаданные функции и единственный timing без повторного traceback."""

        def report(start: float, status: str) -> None:
            """Записывает длительность только с разрешёнными низкокардинальными полями."""
            logging.getLogger(function.__module__).info(
                "operation_timing",
                extra={
                    "operation": operation,
                    "duration_ms": (perf_counter() - start) * 1000,
                    "status": status,
                },
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args, **kwargs):
                """Измеряет асинхронный вызов без перехвата отмены задачи."""
                start, status = perf_counter(), "error"
                try:
                    result = await function(*args, **kwargs)
                    status = "success"
                    return result
                finally:
                    report(start, status)

            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            """Измеряет синхронный вызов, сохраняя исходные исключения."""
            start, status = perf_counter(), "error"
            try:
                result = function(*args, **kwargs)
                status = "success"
                return result
            finally:
                report(start, status)

        return synchronous

    return decorate
