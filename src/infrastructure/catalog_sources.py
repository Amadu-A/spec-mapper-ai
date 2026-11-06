# src/infrastructure/catalog_sources.py
"""Источники каталога: локальный XML и контрактный полный HTTP XML с защитой SSRF."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from time import time
from urllib.parse import urlsplit

import httpx

from src.domain.catalog_sync import validate_time
from src.domain.models import MapperError, digest
from src.infrastructure.xml_catalog import XmlCatalogSource


async def resolve_addresses(host: str, port: int) -> list[str]:
    """Разрешает DNS вне event loop; HTTP позже подключается именно к проверенному IP."""
    entries = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({entry[4][0] for entry in entries})


class CatalogSourceFactory:
    """Делегирует парсинг прежнему XmlCatalogSource, не создавая альтернативный pipeline."""

    def __init__(self, settings, client: httpx.AsyncClient, local, resolver=resolve_addresses):
        """Принимает server-only Settings, HTTP pool и заменяемый DNS для offline security tests."""
        self.settings, self.client, self.local, self.resolver = settings, client, local, resolver

    def credentials_configured(self, config: dict) -> bool:
        """Публикует только признак наличия сервера credentials, никогда не их значения."""
        if config["auth_type"] == "none":
            return True
        if config["auth_type"] == "bearer":
            return bool(self.settings.onec_catalog_token.get_secret_value())
        return bool(
            self.settings.onec_catalog_username.get_secret_value()
            and self.settings.onec_catalog_password.get_secret_value()
        )

    def identity(self, config: dict) -> str:
        """Связывает probe с источником и ротацией credentials через закрытый HMAC."""
        fields = (
            "source_type",
            "url",
            "method",
            "auth_type",
            "credential_ref",
            "expected_format",
            "export_mode",
            "completion_header",
            "completion_value",
            "timeout_seconds",
            "max_bytes",
        )
        credentials = "\0".join(
            value.get_secret_value()
            for value in (
                self.settings.onec_catalog_username,
                self.settings.onec_catalog_password,
                self.settings.onec_catalog_token,
            )
        )
        secret_id = hmac.new(
            self.settings.session_secret.get_secret_value().encode(),
            credentials.encode(),
            hashlib.sha256,
        ).hexdigest()
        if config.get("source_type") == "local_xml":
            return digest(["local_xml", str(self.settings.data_directory.resolve())])
        return digest(
            [
                {key: config.get(key) for key in fields},
                secret_id,
                self.settings.onec_catalog_contract_confirmed,
                self.settings.onec_catalog_allowed_hosts,
                self.settings.onec_catalog_allowed_networks,
                self.settings.onec_catalog_allowed_ports,
            ]
        )

    def validate_url(self, value: str) -> tuple[str, int]:
        """Разрешает только явно настроенный host/port без userinfo, query и fragment."""
        try:
            parsed = urlsplit(value)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        except (ValueError, UnicodeError) as exc:
            raise MapperError("invalid_source_url", "Некорректный адрес API 1С", 422) from exc
        if (
            not host
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(c) <= 32 for c in value)
        ):
            raise MapperError(
                "invalid_source_url", "Нужен HTTP(S) URL без credentials/query/fragment", 422
            )
        if parsed.scheme == "http" and not self.settings.onec_catalog_allow_http:
            raise MapperError("onec_https_required", "Для источника разрешён только HTTPS", 422)
        if host not in {name.lower() for name in self.settings.onec_catalog_allowed_hosts}:
            raise MapperError("onec_host_not_allowed", "Host 1С не включён в server allowlist", 422)
        if port not in self.settings.onec_catalog_allowed_ports:
            raise MapperError("onec_port_not_allowed", "Порт 1С не включён в server allowlist", 422)
        return host, port

    async def validate(self, config: dict, enabling: bool = False) -> dict:
        """Валидация не вызывает HTTP; неизвестный контракт не может включить auto publish."""
        validate_time(config["time_utc"])
        if config.get("source_type") not in {"local_xml", "http_onec"}:
            raise MapperError("invalid_source_type", "Источник: local_xml или http_onec", 422)
        if config["source_type"] == "local_xml":
            if enabling:
                await self.local.read()
            return config
        if (
            config.get("method") != "GET"
            or config.get("expected_format") != "xml"
            or config.get("export_mode") != "full"
        ):
            raise MapperError(
                "onec_contract_unsupported", "Сейчас поддерживается только GET полного XML", 422
            )
        if (
            config.get("auth_type") not in {"none", "bearer", "basic"}
            or config.get("credential_ref") != "onec_catalog"
        ):
            raise MapperError(
                "invalid_credential_reference",
                "Используйте серверный credential reference onec_catalog",
                422,
            )
        if (
            not 1 <= config.get("timeout_seconds", 0) <= 300
            or not 1024 <= config.get("max_bytes", 0) <= self.settings.max_xml_bytes
        ):
            raise MapperError("invalid_source_limits", "Проверьте timeout и лимит размера XML", 422)
        if config.get("url"):
            self.validate_url(config["url"])
            if config["url"].startswith("http:") and config["auth_type"] != "none":
                raise MapperError(
                    "onec_https_required", "Credentials 1С передаются только по HTTPS", 422
                )
        if enabling:
            if not self.settings.onec_catalog_contract_confirmed:
                raise MapperError(
                    "onec_contract_required", "Ожидается утверждённый HTTP-контракт 1С", 422
                )
            if not config.get("url") or not self.credentials_configured(config):
                raise MapperError(
                    "onec_not_configured", "Не настроены URL или серверные credentials 1С", 422
                )
            if (
                not re.fullmatch(r"[A-Za-z0-9-]{1,64}", config.get("completion_header", ""))
                or not config.get("completion_value")
                or any(c in config["completion_value"] for c in "\r\n")
            ):
                raise MapperError(
                    "onec_completion_required",
                    "Укажите согласованный признак завершённости полной выгрузки",
                    422,
                )
        return config

    async def pinned_url(self, config: dict) -> tuple[httpx.URL, dict, dict]:
        """Проверяет все DNS-ответы и закрепляет соединение, сохраняя TLS SNI/Host."""
        host, port = self.validate_url(config["url"])
        try:
            addresses = await self.resolver(host, port)
            networks = [
                ipaddress.ip_network(value) for value in self.settings.onec_catalog_allowed_networks
            ]
            if not addresses:
                raise ValueError("Нет DNS-адресов")
            for address in addresses:
                ip = ipaddress.ip_address(address)
                ip = getattr(ip, "ipv4_mapped", None) or ip
                explicitly_allowed = address in self.settings.onec_catalog_allowed_hosts or any(
                    ip in network for network in networks if ip.version == network.version
                )
                if (
                    ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_unspecified
                    or (not ip.is_global and not explicitly_allowed)
                ):
                    raise MapperError(
                        "onec_address_not_allowed", "DNS 1С указывает на запрещённый IP", 422
                    )
        except (OSError, ValueError) as exc:
            raise MapperError(
                "onec_dns_unavailable", "Не удалось безопасно разрешить адрес 1С", 503
            ) from exc
        original = httpx.URL(config["url"])
        return (
            original.copy_with(host=addresses[0]),
            {"Host": original.netloc.decode()},
            {"sni_hostname": host},
        )

    def source(self, config: dict, progress):
        """Создаёт независимый адаптер, чтобы параллельные jobs не меняли общий mutable source."""
        if config.get("source_type", "local_xml") == "local_xml":
            return self.local
        return OneCHttpCatalogSource(self, config, progress)

    async def probe(self, config: dict) -> dict:
        """Проверяет источник/полный XML, но не stage/activate PostgreSQL или Qdrant."""
        try:
            await self.validate(config, enabling=True)

            async def progress(stage, **details):
                """Probe не создаёт job и не сохраняет XML в журнале."""
                return None

            parsed = await self.source(config, progress).read()
            return {
                "state": "confirmed",
                "count": len(parsed.items),
                "source_hash": parsed.source_hash,
                "source_identity": self.identity(config),
            }
        except MapperError as exc:
            return {
                "state": "awaiting_contract" if exc.code == "onec_contract_required" else "error",
                "error_code": exc.code,
                "source_identity": self.identity(config),
            }

    async def cleanup(self) -> None:
        """Удаляет только старые собственные download-директории после смерти процесса."""
        await asyncio.to_thread(self._cleanup)

    def _cleanup(self) -> None:
        """Проверяет resolved boundary и symlink перед удалением осиротевшего staging."""
        root = self.settings.catalog_staging_directory.resolve()
        if not root.exists():
            return
        for path in root.glob("onec-download-*"):
            if (
                path.is_dir()
                and not path.is_symlink()
                and path.resolve().parent == root
                and path.stat().st_mtime < time() - self.settings.catalog_import_timeout_seconds
            ):
                shutil.rmtree(path)


class OneCHttpCatalogSource:
    """Полный потоковый XML: allowlist, pinned DNS, лимиты, completion marker и прежний parser."""

    def __init__(self, factory: CatalogSourceFactory, config: dict, progress):
        """Фиксирует открытые параметры job; секреты остаются только в server Settings."""
        self.factory, self.config, self.progress = factory, dict(config), progress

    async def read(self):
        """Удаляет временный файл при любом исходе и возвращает только полностью проверенный XML."""
        factory, config = self.factory, self.config
        await factory.validate(config, enabling=True)
        if config.get("_source_identity", factory.identity(config)) != factory.identity(config):
            raise MapperError(
                "onec_configuration_changed",
                "Параметры источника изменились; создайте новый запуск",
                422,
            )
        await self.progress("fetching")
        try:
            async with asyncio.timeout(config["timeout_seconds"]):
                url, headers, extensions = await factory.pinned_url(config)
                headers["Accept-Encoding"] = "identity"
                headers["Accept"] = "application/xml, text/xml"
                headers["Connection"] = "close"
                auth = None
                if config["auth_type"] == "bearer":
                    headers["Authorization"] = (
                        "Bearer " + factory.settings.onec_catalog_token.get_secret_value()
                    )
                elif config["auth_type"] == "basic":
                    auth = httpx.BasicAuth(
                        factory.settings.onec_catalog_username.get_secret_value(),
                        factory.settings.onec_catalog_password.get_secret_value(),
                    )
                root = factory.settings.catalog_staging_directory
                await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="onec-download-", dir=root) as temporary:
                    os.chmod(temporary, 0o700)
                    path = Path(temporary) / "catalog.xml"
                    async with factory.client.stream(
                        "GET",
                        url,
                        headers=headers,
                        extensions=extensions,
                        auth=auth,
                        timeout=config["timeout_seconds"],
                        follow_redirects=False,
                    ) as response:
                        self._validate_response(response)
                        await self.progress("downloading", downloaded_bytes=0)
                        count = 0
                        bucket = 0
                        with path.open("xb") as stream:
                            os.chmod(path, 0o600)
                            async for chunk in response.aiter_bytes(65536):
                                count += len(chunk)
                                if count > config["max_bytes"]:
                                    raise MapperError(
                                        "onec_response_too_large",
                                        "XML превышает настроенный лимит",
                                        422,
                                    )
                                await asyncio.to_thread(stream.write, chunk)
                                if count // 8388608 != bucket:
                                    bucket = count // 8388608
                                    await self.progress("downloading", downloaded_bytes=count)
                        length = response.headers.get("content-length")
                        if not count or length is not None and int(length) != count:
                            raise MapperError(
                                "onec_incomplete_response", "Получена неполная выгрузка 1С", 422
                            )
                    await self.progress("validating", downloaded_bytes=count)
                    return await XmlCatalogSource(
                        Path(temporary), config["max_bytes"], factory.settings.duplicate_code_policy
                    ).read()
        except MapperError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            raise MapperError(
                "onec_source_unavailable", "Не удалось полностью получить XML из 1С", 503
            ) from exc

    def _validate_response(self, response: httpx.Response) -> None:
        """Требует согласованный completion marker, полный 200 и поддерживаемый content type."""
        if response.status_code in {401, 403}:
            raise MapperError("onec_unauthorized", "1С отклонила авторизацию источника", 422)
        if response.status_code != 200:
            raise MapperError(
                "onec_http_error",
                "Источник 1С не вернул полный HTTP 200",
                503 if response.status_code >= 500 else 422,
            )
        if (
            response.headers.get(self.config["completion_header"])
            != self.config["completion_value"]
            or response.headers.get("content-range")
            or response.headers.get("content-encoding", "identity") not in {"", "identity"}
        ):
            raise MapperError(
                "onec_incomplete_response", "Не подтверждена завершённость полной XML-выгрузки", 422
            )
        if response.headers.get("content-type", "").split(";")[0].strip().lower() not in {
            "application/xml",
            "text/xml",
            "application/octet-stream",
        }:
            raise MapperError(
                "onec_invalid_content_type", "Ожидался полный XML, получен другой content type", 422
            )
        length = response.headers.get("content-length")
        if length and int(length) > self.config["max_bytes"]:
            raise MapperError("onec_response_too_large", "XML превышает настроенный лимит", 422)
