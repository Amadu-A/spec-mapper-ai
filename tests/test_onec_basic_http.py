# tests/test_onec_basic_http.py
"""Адресные HTTP mock проверки согласованного Basic с пустым паролем и прежних safeguards."""

from base64 import b64decode

import httpx
import pytest
from pydantic import SecretStr
from test_catalog_json import card, progress

from src.domain.catalog_sync import sync_defaults
from src.domain.models import MapperError
from src.infrastructure.catalog_sources import CatalogSourceFactory
from src.infrastructure.xml_catalog import XmlCatalogSource

BASE = "http://192.168.10.30/erp_update/hs/catalog"


def configured(settings, tmp_path, handler, *, url=BASE + "/export", addresses=None):
    """Готовит только MockTransport; реальные сетевые запросы из этих тестов невозможны."""
    settings.onec_catalog_allow_http = True
    settings.onec_catalog_contract_confirmed = True
    settings.onec_catalog_allowed_hosts = ["192.168.10.30"]
    settings.onec_catalog_allowed_networks = ["192.168.10.30/32"]
    settings.onec_catalog_allowed_ports = [80, 443]
    settings.onec_catalog_username = SecretStr("ЩипцовА")
    settings.onec_catalog_password = SecretStr("")
    settings.catalog_staging_directory = tmp_path / "spool"
    config = sync_defaults(settings) | {
        "source_type": "http_onec",
        "url": url,
        "auth_type": "basic",
        "expected_format": "json",
        "timeout_seconds": 120,
        "completion_header": "X-Catalog-Complete",
        "completion_value": "true",
    }

    async def resolver(host, port):
        """Закрепляет явно разрешённый адрес без обращения к настоящему DNS."""
        return addresses or ["192.168.10.30"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    factory = CatalogSourceFactory(
        settings, client, XmlCatalogSource(tmp_path, 100000, "quarantine"), resolver
    )
    return factory, config, client


async def test_basic_empty_password_validate_probe_download_batch(settings, tmp_path):
    """Кириллическое имя и пустой пароль проходят весь адаптер, не меняя настройки и каталог."""
    rows = [card(), card(code="000002", deleted=True)]
    calls = []

    def handler(request):
        """Проверяет UTF-8 Basic, timeout 120 и фиксированный endpoint batch."""
        calls.append(request)
        encoded = request.headers["authorization"].split(" ", 1)
        assert encoded[0] == "Basic" and b64decode(encoded[1]) == "ЩипцовА:".encode()
        assert request.extensions["timeout"]["read"] == 120
        assert request.url.host == "192.168.10.30"
        assert request.headers["host"] == "192.168.10.30"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "POST":
            assert request.url.path == "/erp_update/hs/catalog/items/batch"
            return httpx.Response(200, json={"items": [rows[0]]})
        assert request.url.path == "/erp_update/hs/catalog/export"
        return httpx.Response(200, json={"items": rows}, headers={"X-Catalog-Complete": "true"})

    factory, config, client = configured(settings, tmp_path, handler)
    try:
        assert factory.credentials_configured(config)
        await factory.validate(config, enabling=True)
        probe = await factory.probe(config)
        assert probe["state"] == "confirmed" and probe["count"] == 1
        parsed = await factory.source(config, progress).read()
        assert len(parsed.records) == 2 and parsed.statistics["deleted_rows"] == 1
        assert parsed.items[0].article == "0000012"
        batch = await factory.batch(config, [rows[0]["guid"]])
        assert len(batch) == 1 and batch[0].onec_guid == rows[0]["guid"]
        assert len(calls) == 3
        assert settings.onec_catalog_password.get_secret_value() == ""
        assert not list(settings.catalog_staging_directory.iterdir())
    finally:
        await client.aclose()


@pytest.mark.parametrize("auth_type", ["none", "basic", "bearer"])
async def test_http_remains_denied_without_explicit_opt_in(settings, tmp_path, auth_type):
    """Выключенный флаг отклоняет HTTP до auth/download независимо от типа credentials."""
    factory, config, client = configured(settings, tmp_path, lambda _: pytest.fail("HTTP запрещён"))
    settings.onec_catalog_allow_http = False
    config["auth_type"] = auth_type
    try:
        with pytest.raises(MapperError) as error:
            await factory.validate(config, enabling=True)
        assert error.value.code == "onec_https_required"
    finally:
        await client.aclose()


@pytest.mark.parametrize("auth_type", ["basic", "bearer"])
async def test_https_credentials_are_unchanged(settings, tmp_path, auth_type):
    """Обычные Basic password и Bearer по HTTPS продолжают работать без HTTP opt-in."""
    calls = []

    def handler(request):
        """Проверяет прежнюю сериализацию непустых credentials исключительно в mock."""
        calls.append(request)
        if auth_type == "basic":
            assert (
                b64decode(request.headers["authorization"].split()[1])
                == "ЩипцовА:mock-pass".encode()
            )
        else:
            assert request.headers["authorization"] == "Bearer mock-token"
        return httpx.Response(200, json={"items": [card()]}, headers={"X-Catalog-Complete": "true"})

    factory, config, client = configured(
        settings, tmp_path, handler, url=BASE.replace("http:", "https:") + "/export"
    )
    settings.onec_catalog_allow_http = False
    settings.onec_catalog_password = SecretStr("mock-pass")
    settings.onec_catalog_token = SecretStr("mock-token")
    config["auth_type"] = auth_type
    try:
        assert (await factory.probe(config))["state"] == "confirmed"
        assert len(calls) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "failure",
    ["host", "port", "address", "redirect", "username", "contract", "bearer", "bearer_uppercase"],
)
async def test_basic_opt_in_keeps_existing_security_boundaries(settings, tmp_path, failure):
    """Basic исключение не отключает allowlist, DNS pin, redirect, contract и Bearer TLS."""
    calls = []

    def handler(request):
        """Redirect не должен передать Basic следующему адресу."""
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    factory, config, client = configured(
        settings, tmp_path, handler, addresses=["10.55.0.1"] if failure == "address" else None
    )
    expected = {
        "host": "onec_host_not_allowed",
        "port": "onec_port_not_allowed",
        "address": "onec_address_not_allowed",
        "redirect": "onec_http_error",
        "username": "onec_not_configured",
        "contract": "onec_contract_required",
        "bearer": "onec_https_required",
        "bearer_uppercase": "onec_https_required",
    }[failure]
    if failure == "host":
        settings.onec_catalog_allowed_hosts = ["other.test"]
    elif failure == "port":
        settings.onec_catalog_allowed_ports = [443]
    elif failure == "username":
        settings.onec_catalog_username = SecretStr("")
    elif failure == "contract":
        settings.onec_catalog_contract_confirmed = False
    elif failure in {"bearer", "bearer_uppercase"}:
        config["auth_type"] = "bearer"
        if failure == "bearer_uppercase":
            config["url"] = config["url"].replace("http:", "HTTP:", 1)
    try:
        with pytest.raises(MapperError) as error:
            await factory.source(config, progress).read()
        assert error.value.code == expected
        assert len(calls) == (1 if failure == "redirect" else 0)
    finally:
        await client.aclose()
