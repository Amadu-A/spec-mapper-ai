# src/config.py
"""Единственный источник параметров: окружение процесса > .env > .env.example > defaults."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Типизированные настройки приложения и скриптов без чтения секретов в других слоях."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        env_nested_delimiter="__",
        hide_input_in_errors=True,
    )
    app_bind: str = "0.0.0.0"
    app_port: int = Field(8087, ge=1024, le=65535)
    app_public_base_url: str = "http://192.168.55.3:8087"
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "spec_mapper"
    database_user: str = "spec_mapper"
    database_password: SecretStr = SecretStr("")
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "spec_mapper"
    rabbitmq_vhost: str = "spec_mapper"
    rabbitmq_queue: str = "spec_mapper.jobs"
    rabbitmq_password: SecretStr = SecretStr("")
    shared_embedding_base_url: str = "http://shared-embedding:8000/v1"
    shared_embedding_model: str = "shared-embedding"
    embedding_revision: str = ""
    embedding_batch_size: int = Field(32, ge=1, le=256)
    embedding_timeout_seconds: int = Field(90, ge=1, le=300)
    qdrant_url: str = "http://qdrant:6333"
    qdrant_alias: str = "spec_mapper_catalog"
    extracting_base_url: str = "http://spec-extracting-ai-api-1:8083"
    extracting_api_key: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    catalog_admin_key: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    cookie_secure: bool = False
    data_directory: Path = Path("data")
    duplicate_code_policy: str = "quarantine"
    catalog_daily_sync: bool = False
    catalog_sync_seconds: int = Field(86400, ge=60)
    retention_seconds: int = Field(3600, ge=60, le=86400)
    draft_ttl_seconds: int = Field(3600, ge=60, le=3600)
    catalog_import_timeout_seconds: int = Field(86400, ge=3600, le=604800)
    poll_seconds: float = Field(3, ge=0.05, le=60)
    upstream_timeout_seconds: int = Field(900, ge=1, le=3300)
    http_timeout_seconds: int = Field(30, ge=1, le=120)
    max_result_bytes: int = Field(15000000, ge=1024, le=50000000)
    max_xml_bytes: int = Field(200000000, ge=1024)
    max_items: int = Field(10000, ge=1, le=50000)
    top_k: int = Field(5, ge=1, le=30)
    match_threshold: float = Field(0.88, ge=0, le=1)
    match_margin: float = Field(0.05, ge=0, le=1)
    worker_concurrency: int = Field(2, ge=1, le=8)
    lease_seconds: int = Field(120, ge=30)
    max_attempts: int = Field(3, ge=1, le=5)
    rate_limit_per_minute: int = Field(120, ge=10)
    log_level: str = "INFO"

    @field_validator("api_keys", mode="before")
    @classmethod
    def empty_api_keys(cls, value):
        """Пустой JSON null от Compose означает отсутствие именованных ключей до bootstrap."""
        return {} if value is None else value

    @model_validator(mode="after")
    def check_policy(self) -> Settings:
        """Запрещает молчаливое использование неизвестной политики дубликатов и порта upstream."""
        if self.duplicate_code_policy not in {"reject", "quarantine"}:
            raise ValueError("duplicate_code_policy: reject или quarantine")
        if self.app_port == 8083:
            raise ValueError("Порт 8083 принадлежит spec-extracting-ai")
        public = urlsplit(self.app_public_base_url)
        if (
            public.scheme not in {"http", "https"}
            or not public.hostname
            or public.username
            or public.password
        ):
            raise ValueError("APP_PUBLIC_BASE_URL: требуется HTTP(S) адрес без credentials")
        if public.path not in {"", "/"} or public.query or public.fragment:
            raise ValueError("APP_PUBLIC_BASE_URL: укажите адрес без пути, query и fragment")
        values = [value.get_secret_value() for value in self.api_keys.values()]
        if len(set(values)) != len(values) or any(
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) for name in self.api_keys
        ):
            raise ValueError("API_KEYS: нужны уникальные ключи и безопасные имена владельцев")
        if self.api_key.get_secret_value() and self.api_key.get_secret_value() in values:
            raise ValueError("Legacy API_KEY должен отличаться от именованных API_KEYS")
        return self

    @property
    def database_url(self) -> str:
        """Собирает приватный DSN с экранированием; адрес запрещён к журналированию."""
        password = quote(self.database_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{quote(self.database_user, safe='')}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def amqp_url(self) -> str:
        """Собирает приватный адрес выделенного vhost общего RabbitMQ."""
        return (
            f"amqp://{quote(self.rabbitmq_user, safe='')}:"
            f"{quote(self.rabbitmq_password.get_secret_value(), safe='')}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{quote(self.rabbitmq_vhost, safe='')}"
        )

    def require_secrets(self) -> None:
        """Останавливает запуск при незаданных проектных секретах, не выводя их значения."""
        for name in (
            "database_password",
            "rabbitmq_password",
            "session_secret",
            "catalog_admin_key",
        ):
            if len(getattr(self, name).get_secret_value()) < 24:
                raise ValueError(f"Необходимо задать секрет {name} длиной от 24 символов")
        if not self.api_key.get_secret_value() and not self.api_keys:
            raise ValueError("Необходимо задать API_KEYS либо прежний API_KEY")
        for value in [
            *self.api_keys.values(),
            *([self.api_key] if self.api_key.get_secret_value() else []),
        ]:
            if len(value.get_secret_value()) < 24:
                raise ValueError("Каждый интеграционный API-ключ должен содержать от 24 символов")
