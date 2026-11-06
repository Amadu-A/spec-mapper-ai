# src/application/catalog_management.py
"""Use-cases GUI-управления каталогом; один pipeline, роли проверяет HTTP boundary."""

from __future__ import annotations

from src.application.sync_ports import CatalogSources, SyncRepository
from src.domain.catalog_sync import sync_defaults, validate_time
from src.domain.models import MapperError
from src.observability import timed

CONFIG_FIELDS = (
    "enabled",
    "time_utc",
    "source_type",
    "url",
    "method",
    "auth_type",
    "credential_ref",
    "expected_format",
    "export_mode",
    "timeout_seconds",
    "max_bytes",
    "completion_header",
    "completion_value",
)


class CatalogManagement:
    """Оркестрирует настройки/probe/enqueue без SQL, HTTP-клиентов и второй state machine."""

    def __init__(
        self, repository: SyncRepository, sources: CatalogSources, settings, clock
    ) -> None:
        """Принимает порты и часы; .env применяется только для bootstrap и server-only
        ограничений.
        """
        self.repository, self.sources, self.defaults, self.clock = (
            repository,
            sources,
            settings,
            clock,
        )

    def config(self, value: dict) -> dict:
        """Убирает UI/probe метаданные из immutable конфигурации отдельного импорта."""
        return {key: value[key] for key in CONFIG_FIELDS}

    def valid_probe(self, value: dict, config: dict | None = None, fresh: bool = True) -> bool:
        """Probe привязан к revision, identity credentials и ограниченному сроку актуальности."""
        probe = value.get("probe", {})
        return (
            probe.get("state") == "confirmed"
            and probe.get("revision") == value["revision"]
            and probe.get("source_identity") == self.sources.identity(config or value)
            and (not fresh or self.clock() - probe.get("checked_at", 0) < 3600)
        )

    async def settings(self) -> dict:
        """Возвращает только открытые параметры/признаки secret_configured для operator UI."""
        value = await self.repository.settings()
        config = self.config(value)
        configured, error = True, None
        try:
            await self.sources.validate(config, enabling=config["source_type"] == "http_onec")
        except MapperError as exc:
            configured, error = False, exc.code
        probe = {
            key: item for key, item in value.get("probe", {}).items() if key != "source_identity"
        }
        if config["source_type"] == "http_onec" and not self.valid_probe(value, fresh=False):
            configured = False
        return {
            **config,
            "revision": value["revision"],
            "updated_at": value["updated_at"],
            "next_run_at": value["next_run_at"],
            "is_configured": configured,
            "secret_configured": self.sources.credentials_configured(config),
            "contract_confirmed": self.defaults.onec_catalog_contract_confirmed,
            "probe": probe,
            "probe_current": self.valid_probe(value),
            "configuration_error": error,
        }

    @timed("catalog_settings_save")
    async def save(self, config: dict, revision: int, actor: str) -> dict:
        """Сохранение не запускает импорт; включить непроверенный HTTP источник нельзя."""
        validate_time(config["time_utc"])
        await self.sources.validate(config, enabling=config["enabled"])
        current = await self.repository.settings()
        if (
            config["enabled"]
            and config["source_type"] == "http_onec"
            and not self.valid_probe(current, config)
        ):
            raise MapperError(
                "onec_probe_required", "Сначала сохраните источник и проверьте подключение", 422
            )
        config = {**config, "_source_identity": self.sources.identity(config)}
        await self.repository.save(config, revision, actor)
        return await self.settings()

    @timed("catalog_connection_test")
    async def connection_test(self) -> dict:
        """Проверка не публикует каталог; результат не маскирует ожидание реального контракта."""
        current = await self.repository.settings()
        result = await self.sources.probe(self.config(current))
        await self.repository.probe(current["revision"], result)
        return await self.settings()

    @timed("catalog_trigger")
    async def trigger(self, owner: str, key: str, source_type: str | None = None) -> dict:
        """Старый вызов без тела импортирует локальный XML, GUI может явно выбрать
        сохранённый source.
        """
        if source_type is None:
            config = sync_defaults(self.defaults)
            config.update(enabled=False, source_type="local_xml")
        else:
            current = await self.repository.settings()
            config = {**self.config(current), "source_type": source_type}
            if source_type == "http_onec" and not self.valid_probe(current, config, fresh=False):
                raise MapperError(
                    "onec_probe_required", "Нет актуального подтверждения источника 1С", 422
                )
        await self.sources.validate(config, enabling=config["source_type"] == "http_onec")
        config["_source_identity"] = self.sources.identity(config)
        return await self.repository.trigger(owner, key, config, "manual")

    async def tick(self) -> dict | None:
        """Worker проверяет UTC даже без browser; выключенный scheduler не опрашивает 1С."""
        current = await self.repository.settings()
        if (
            not current["enabled"]
            or current["next_run_at"] is None
            or current["next_run_at"] > self.clock()
        ):
            return None
        config = self.config(current)
        if config["source_type"] == "http_onec" and not self.valid_probe(current, fresh=False):
            return None
        await self.sources.validate(config, enabling=config["source_type"] == "http_onec")
        return await self.repository.schedule(self.sources.identity(config), current["revision"])

    async def record_success(self, config: dict, result: dict) -> None:
        """Настоящий успешный HTTP импорт обновляет probe, если источник за время работы не
        изменили.
        """
        current = await self.repository.settings()
        if self.sources.identity(current) == config.get("_source_identity"):
            try:
                await self.repository.probe(
                    current["revision"],
                    {
                        "state": "confirmed",
                        "source_identity": config["_source_identity"],
                        "count": result["count"],
                        "source_hash": result["source_hash"],
                        "_actor": "worker",
                    },
                )
            except MapperError as exc:
                if exc.code != "revision_conflict":
                    raise
