# src/application/sync_ports.py
"""Порты управления каталогом; конкретные SQL/HTTP и секреты остаются в infrastructure."""

from __future__ import annotations

from typing import Protocol

from src.application.ports import CatalogSource


class SyncRepository(Protocol):
    """Атомарные настройки, запуск с outbox и технический аудит без исходного XML."""

    async def settings(self) -> dict:
        """Читает/однократно инициализирует singleton с bootstrap defaults."""
        ...

    async def save(self, config: dict, revision: int, actor: str) -> dict:
        """Сохраняет конфигурацию CAS и аудит без автоматического запуска импорта."""
        ...

    async def trigger(
        self,
        owner: str,
        key: str,
        config: dict,
        initiator: str,
        minimum_interval: int = 0,
        expected_revision: int | None = None,
    ) -> dict:
        """Создаёт один catalog job и audit run в общей транзакции с защитой от дублей."""
        ...

    async def refresh_status(self) -> dict:
        """Читает глобальный минутный интервал и обезличенный прогресс активного импорта."""
        ...

    async def public_refresh_replay(self, owner: str, key: str) -> dict | None:
        """Читает только свой уже принятый public refresh без запуска нового импорта."""
        ...

    async def schedule(self, source_identity: str, revision: int | None = None) -> dict | None:
        """Атомарно захватывает один UTC-слот; совмещает его с уже начатым manual run."""
        ...

    async def probe(self, revision: int, result: dict) -> dict:
        """Фиксирует probe только для неизменённой конфигурационной revision."""
        ...

    async def history(self, offset: int, limit: int, state: str, source: str) -> dict:
        """Возвращает страницу технических импортов, не содержащую строки номенклатуры."""
        ...

    async def report(self, run_id: str) -> dict:
        """Возвращает долговечный отчёт реального запуска либо not_found."""
        ...

    async def event(self, job_id: str, stage: str, details: dict) -> None:
        """Добавляет агрегированный переход этапа; повторяющиеся пачки не порождают log spam."""
        ...

    async def summary(self, owner: str | None) -> dict:
        """Читает неперсонализированные KPI: глобальные только при owner=None у оператора."""
        ...

    async def jobs_page(
        self, owner: str | None, offset: int, limit: int, state: str, order: str
    ) -> dict:
        """Возвращает только метаданные заданий; source/result и права owner не изменяются."""
        ...

    async def touch(self, name: str) -> None:
        """Сохраняет технический heartbeat/успешную inbound активность без API-ключа."""
        ...

    async def activity(self, name: str) -> float | None:
        """Читает время активности для честного индикатора интеграции/worker."""
        ...

    async def cleanup(self) -> None:
        """Удаляет только просроченные audit/metrics/tickets по отдельной retention."""
        ...


class CatalogSources(Protocol):
    """Фабрика безопасного источника, общая для probe и уже существующей синхронизации."""

    async def validate(self, config: dict, enabling: bool = False) -> dict:
        """Проверяет открытые настройки и готовность источника без запроса к HTTP 1С."""
        ...

    def credentials_configured(self, config: dict) -> bool:
        """Возвращает только наличие server credential без чтения его значения внешним кодом."""
        ...

    def identity(self, config: dict) -> str:
        """Вычисляет identity настроек/credential reference без раскрытия секрета."""
        ...

    def source(self, config: dict, progress) -> CatalogSource:
        """Возвращает отдельный адаптер на задание, без изменения общего mutable source."""
        ...

    async def probe(self, config: dict) -> dict:
        """Проверяет получение/валидацию полного источника, не создавая DB/Qdrant snapshot."""
        ...

    async def batch(self, config: dict, guids: list[str]) -> list:
        """Безопасно получает текущие карточки строго по запрошенным настоящим GUID."""
        ...
