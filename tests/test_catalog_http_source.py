# tests/test_catalog_http_source.py
"""Mock HTTP full XML: SSRF, авторизация, обрывы, карантин и сохранение старого индекса."""

import httpx
import pytest
from pydantic import SecretStr

from src.domain.catalog_sync import sync_defaults
from src.domain.models import MapperError
from src.infrastructure.catalog_sources import CatalogSourceFactory
from src.infrastructure.xml_catalog import XmlCatalogSource

XML = (
    '<СправочникНоменклатура><Позиция код="00001"><Наименование>Клапан</Наименование>'
    "<ЕдиницаИзмерения>шт</ЕдиницаИзмерения><ПометкаУдаления>Нет</ПометкаУдаления>"
    "</Позиция></СправочникНоменклатура>"
).encode()


async def progress(stage, **details):
    """Принимает фактические этапы адаптера без публикации snapshot."""


def setup(settings, tmp_path, handler, addresses=None):
    """Разрешает только тестовый host и pinned private IP через явную server allowlist."""
    settings.catalog_staging_directory = tmp_path / "staging"
    settings.onec_catalog_allowed_hosts = ["onec.test"]
    settings.onec_catalog_allowed_networks = ["192.168.55.0/24"]
    settings.onec_catalog_contract_confirmed = True
    settings.onec_catalog_token = SecretStr("only-server-secret")
    config = sync_defaults(settings)
    config.update(
        source_type="http_onec",
        url="https://onec.test/catalog",
        completion_header="X-Test-Full",
        completion_value="yes",
        auth_type="bearer",
        expected_format="xml",
    )

    async def resolve(host, port):
        """Подменяет только DNS; HTTP-запрос проходит реальный httpx mock transport."""
        return addresses or ["192.168.55.10"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    factory = CatalogSourceFactory(
        settings, client, XmlCatalogSource(tmp_path, 100000, "quarantine"), resolve
    )
    return factory, config, client


async def test_http_full_xml_stream_auth_sni_cleanup(settings, tmp_path):
    """Host/SNI сохраняются при закреплённом IP, credentials не попадают в XML/отчёт."""
    requests = []
    stages = []

    def handler(request):
        """Возвращает полный fixture с явным mock-признаком завершённости."""
        requests.append(request)
        return httpx.Response(
            200, content=XML, headers={"Content-Type": "application/xml", "X-Test-Full": "yes"}
        )

    factory, config, client = setup(settings, tmp_path, handler)

    async def collect(stage, **details):
        """Проверяет последовательность реальных этапов скачивания."""
        stages.append(stage)

    try:
        parsed = await factory.source(config, collect).read()
        assert parsed.items[0].code == "00001"
        request = requests[0]
        assert request.url.host == "192.168.55.10"
        assert request.headers["host"] == "onec.test"
        assert request.headers["authorization"] == "Bearer only-server-secret"
        assert request.extensions["sni_hostname"] == "onec.test"
        assert stages == ["fetching", "downloading", "validating"]
        assert not list(settings.catalog_staging_directory.iterdir())
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (302, {"Location": "http://169.254.169.254/"}, b""),
        (401, {}, b"private diagnostic"),
        (403, {}, b""),
        (500, {}, b""),
        (206, {"X-Test-Full": "yes"}, XML),
        (200, {}, XML),
        (200, {"X-Test-Full": "yes", "Content-Range": "bytes 0-10/100"}, XML),
        (200, {"X-Test-Full": "yes", "Content-Length": "9999999"}, XML),
        (200, {"X-Test-Full": "yes"}, XML[:-15]),
        (
            200,
            {"X-Test-Full": "yes"},
            b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>',
        ),
        (200, {"X-Test-Full": "yes", "Content-Type": "application/json"}, b"{}"),
    ],
)
async def test_unverified_http_preserves_active_catalog(system, tmp_path, status, headers, body):
    """Любая сомнительная выгрузка завершается до смены активных PostgreSQL/Qdrant."""
    container, *_ = system
    before = await container.catalog.status()
    alias = container.index.alias

    def handler(request):
        """Возвращает отказ или частичный XML без сетевых запросов наружу."""
        return httpx.Response(
            status, content=body, headers={"Content-Type": "application/xml", **headers}
        )

    factory, config, client = setup(container.settings, tmp_path, handler)
    try:
        with pytest.raises(MapperError):
            await factory.source(config, progress).read()
        assert (await container.catalog.status())["version"] == before["version"]
        assert container.index.alias == alias
        assert (
            not list(settings_path.iterdir())
            if (settings_path := container.settings.catalog_staging_directory).exists()
            else True
        )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["10.1.1.1"],
        ["192.168.55.10", "127.0.0.1"],
        ["::ffff:127.0.0.1"],
    ],
)
async def test_ssrf_dns_rebinding_rejected(settings, tmp_path, addresses):
    """Все DNS-ответы проверяются; смешанный список не обходит SSRF allowlist."""
    factory, config, client = setup(settings, tmp_path, lambda request: None, addresses)
    try:
        with pytest.raises(MapperError):
            await factory.pinned_url(config)
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@onec.test/catalog",
        "https://onec.test/?token=x",
        "https://evil.test/catalog",
        "http://onec.test/catalog",
        "https://onec.test:8443/catalog",
        "https://onec.test/#secret",
        "file:///etc/passwd",
    ],
)
async def test_unsafe_source_url(settings, tmp_path, url):
    """URL не переносит credentials, query, чужие host/port и неподдерживаемые схемы."""
    factory, config, client = setup(settings, tmp_path, lambda request: None)
    try:
        with pytest.raises(MapperError):
            factory.validate_url(url)
    finally:
        await client.aclose()
