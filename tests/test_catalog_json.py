# tests/test_catalog_json.py
"""Контрактные offline проверки полного JSON 1С, безопасности HTTP и GUID migration."""

import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from src.domain.catalog_sync import sync_defaults
from src.domain.matching import catalog_text
from src.domain.models import MapperError, digest
from src.infrastructure.catalog_sources import CatalogSourceFactory
from src.infrastructure.json_catalog import JsonCatalogSource
from src.infrastructure.xml_catalog import XmlCatalogSource


def card(guid=None, **values):
    """Создаёт синтетическую карточку с обязательными полями реального контракта."""
    return {
        "guid": guid or str(uuid4()),
        "code": "000001",
        "name": "Клапан",
        "full_name": "Клапан DN50 PN16",
        "unit": "шт",
        "article": "0000012",
        "brand": "",
        "manufacturer": "",
        "deleted": False,
        **values,
    }


async def progress(stage, **details):
    """Принимает checkpoints standalone импорта без внешних систем."""


def json_source(tmp_path, rows, **options):
    """Записывает mock JSON в разрешённый pytest staging."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"items": rows}, ensure_ascii=False), encoding="utf-8")
    return JsonCatalogSource(path, 40000000, 70000, **options)


async def test_json_deleted_codes_metadata_and_duplicates(tmp_path):
    """Сохраняет нули/имена, deleted исключает из поиска; все code conflicts карантинит."""
    rows = [card(), card(code="000002", deleted=True), card(code="X"), card(code="X")]
    parsed = await json_source(tmp_path, rows).read()
    assert len(parsed.items) == 1 and len(parsed.records) == 4
    assert parsed.items[0].code == "000001" and parsed.items[0].article == "0000012"
    assert parsed.items[0].name == rows[0]["full_name"]
    assert parsed.items[0].short_name == rows[0]["name"]
    assert parsed.statistics["quarantined_rows"] == 2 and parsed.warnings
    with pytest.raises(MapperError, match="повторный"):
        await json_source(tmp_path, [rows[0], rows[0]]).read()


@pytest.mark.parametrize(
    "mutation", ["bool", "code", "guid", "missing", "unknown", "empty", "giant"]
)
async def test_json_strict_card_contract(tmp_path, mutation):
    """Числа, ложные GUID и лишние поля не нормализуются в успешную выгрузку."""
    row = card()
    if mutation == "bool":
        row["deleted"] = "false"
    elif mutation == "code":
        row["code"] = 1
    elif mutation == "guid":
        row["guid"] = "bad"
    elif mutation == "missing":
        row.pop("full_name")
    elif mutation == "unknown":
        row["price"] = 10
    elif mutation == "empty":
        row["name"] = row["full_name"] = ""
    else:
        row["article"] = "a" * 32001
    with pytest.raises(MapperError):
        await json_source(tmp_path, [row]).read()


@pytest.mark.parametrize(
    "text",
    [
        '{"items":[]}',
        '{"items":[',
        "<html>error</html>",
        '{"items":[],"next":1}',
        '{"items":[' + "[" * 20 + "0" + "]" * 20 + "]}",
    ],
)
async def test_json_truncated_empty_deep_extra_root(tmp_path, text):
    """Отклоняет пустую, частичную, слишком глубокую или неподтверждённую форму JSON."""
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(MapperError):
        await JsonCatalogSource(path, 100000, 100).read()


async def test_large_streaming_json_65000_rows_23mb(tmp_path):
    """Синтетические 65k/23MB читаются bounded parser, без сети и генерации векторов."""
    path = tmp_path / "large.json"
    with path.open("w", encoding="utf-8") as stream:
        stream.write('{"items":[')
        for i in range(65000):
            if i:
                stream.write(",")
            stream.write(
                json.dumps(
                    card(
                        guid=f"00000000-0000-4000-8000-{i + 1:012x}",
                        code=f"{i:08}",
                        full_name="Наименование " + "а" * 80,
                    ),
                    ensure_ascii=False,
                )
            )
        stream.write("]}")
    assert path.stat().st_size > 23000000
    parsed = await JsonCatalogSource(path, 40000000, 65000).read()
    assert len(parsed.items) == 65000 and parsed.items[-1].code == "00064999"
    with pytest.raises(MapperError):
        await JsonCatalogSource(path, 40000000, 64000).read()


async def test_http_json_full_and_batch_share_ssrf_auth_limits(settings, tmp_path):
    """Оба endpoint проходят pinned HTTPS transport; batch не требует full completion marker."""
    rows, calls = [card()], []
    settings.onec_catalog_contract_confirmed = True  # Только изолированный mock transport.
    settings.onec_catalog_allowed_hosts = ["onec.mock"]
    settings.onec_catalog_allowed_networks = ["10.250.0.0/24"]
    settings.onec_catalog_username = SecretStr("mock-readonly")
    settings.onec_catalog_password = SecretStr("mock-password")
    settings.catalog_staging_directory = tmp_path / "spool"
    config = sync_defaults(settings) | {
        "source_type": "http_onec",
        "url": "https://onec.mock/catalog/export",
        "expected_format": "json",
        "auth_type": "basic",
        "completion_header": "X-Catalog-Complete",
        "completion_value": "true",
    }

    async def resolver(host, port):
        """Закрепляет заведомо разрешённый private IP исключительно внутри mock transport."""
        return ["10.250.0.20"]

    def handle(request):
        """Проверяет реальный отправленный контракт GET/export и POST/items/batch."""
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {"guids": [rows[0]["guid"]]}
        return httpx.Response(
            200,
            json={"items": rows},
            headers={"X-Catalog-Complete": "true"} if request.method == "GET" else {},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        factory = CatalogSourceFactory(
            settings, client, XmlCatalogSource(tmp_path, 1000, "quarantine"), resolver
        )
        parsed = await factory.source(config, progress).read()
        assert parsed.context["full_json"] and parsed.items[0].onec_guid == rows[0]["guid"]
        assert (await factory.batch(config, [rows[0]["guid"]]))[0].article == "0000012"
        assert calls[0].headers["host"] == "onec.mock" and calls[1].url.path.endswith(
            "/items/batch"
        )
        assert calls[0].headers["authorization"].startswith("Basic ")
        assert not list(settings.catalog_staging_directory.iterdir())


async def test_guid_migration_metadata_only_semantic_cache_and_code_change(system, tmp_path):
    """Сохраняет legacy UUID и снимки; новые внешние GUID/код не вызывают embedding."""
    container, legacy, sync, _, _ = system
    row = card(code=legacy.code, name=legacy.name, full_name=legacy.name, article="")
    parsed_source = json_source(tmp_path, [row])
    calls = container.embeddings.calls
    first = await sync.execute(progress, parsed_source)
    stored = await container.catalog.items(first["version"], [legacy.guid])
    assert stored[legacy.guid].onec_guid == row["guid"]
    assert container.embeddings.calls == calls and first["metadata_only_count"] == 1
    row["code"] = "000000999"
    await sync.execute(progress, json_source(tmp_path, [row]))
    state = await container.catalog.status()
    assert await container.catalog.guids(state["version"]) == [legacy.guid]
    assert container.embeddings.calls == calls
    row["article"] = "00007"
    second = await sync.execute(progress, json_source(tmp_path, [row]))
    assert container.embeddings.calls == calls + 1 and second["embedded_count"] == 1
    row["full_name"] += " Новое"
    await sync.execute(progress, json_source(tmp_path, [row]))
    assert container.embeddings.calls == calls + 2


async def test_legacy_cache_promoted_without_reembedding_after_index_repair(system, tmp_path):
    """Реальный V3 ключ кэша переносится, даже если первый V4 sync менял только GUID."""
    from sqlalchemy import delete

    from src.infrastructure.database import VectorCacheRow

    container, legacy, sync, _, _ = system
    # Эмулируем только существующий V3 cache key вместо созданного fixture V4.
    async with container.catalog.sessions.begin() as session:
        await session.execute(delete(VectorCacheRow))
    await container.catalog.cache_vectors({digest(["fp1", legacy.legacy_content_hash]): [1.0, 0.0]})
    assert catalog_text(legacy)
    calls = container.embeddings.calls
    row = card(code=legacy.code, name=legacy.name, full_name=legacy.name, article="")
    first = await sync.execute(progress, json_source(tmp_path, [row]))
    container.index.collections.pop(first["collection"])
    await sync.execute(progress, json_source(tmp_path, [row]))
    assert container.embeddings.calls == calls


async def test_full_shrink_guard_preserves_alias_and_requires_exact_hash(system, tmp_path):
    """Сокращение более 10% требует адресного server-side подтверждения текущего файла."""
    container, item, sync, local, _ = system
    local.items = [replace(item, guid=str(uuid4()), code=f"{i:08}") for i in range(20)]
    local.source_hash = "many"
    before = await sync.execute(progress)
    source = json_source(tmp_path, [card()])
    with pytest.raises(MapperError) as error:
        await sync.execute(progress, source)
    assert error.value.code == "catalog_shrink_guard"
    assert (await container.catalog.status())["version"] == before["version"]
    parsed = await source.read()
    parsed.context["shrink_ack"] = parsed.source_hash

    class Acknowledged:
        """Подменяет только уже проверенное адресное операторское подтверждение."""

        async def read(self):
            """Возвращает проверенный snapshot для общего use-case."""
            return parsed

    assert (await sync.execute(progress, Acknowledged()))["count"] == 1


@pytest.mark.parametrize(
    "failure",
    ["marker", "mime", "truncated", "html", "charset", "redirect", "auth", "limit", "length"],
)
async def test_json_http_rejects_untrusted_full_responses(settings, tmp_path, failure):
    """JSON download сохраняет общие SSRF/stream safeguards и не принимает partial/HTML/redirect."""
    settings.onec_catalog_contract_confirmed = True  # Только MockTransport.
    settings.onec_catalog_allowed_hosts = ["onec.mock"]
    settings.onec_catalog_allowed_networks = ["10.250.0.0/24"]
    settings.catalog_staging_directory = tmp_path / "spool"
    config = sync_defaults(settings) | {
        "source_type": "http_onec",
        "url": "https://onec.mock/export",
        "expected_format": "json",
        "completion_header": "X-Catalog-Complete",
        "completion_value": "true",
        "auth_type": "none",
    }
    body = json.dumps({"items": [card()]}).encode()
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Catalog-Complete": "true"}
    status = 200
    if failure == "marker":
        headers.pop("X-Catalog-Complete")
    elif failure == "mime":
        headers["Content-Type"] = "text/html"
    elif failure == "truncated":
        body = body[:-5]
    elif failure == "html":
        body = b"<html>error</html>"
    elif failure == "charset":
        headers["Content-Type"] = "application/json; charset=latin1"
    elif failure == "redirect":
        status, headers["Location"] = 302, "http://169.254.169.254/"
    elif failure == "auth":
        status = 401
    elif failure == "limit":
        config["max_bytes"], body = 1024, b"x" * 1025
    else:
        headers["Content-Length"] = str(len(body) + 100)

    async def resolver(host, port):
        """Использует явный private IP только для изолированного mock transport."""
        return ["10.250.0.10"]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=body, headers=headers)
        )
    ) as client:
        factory = CatalogSourceFactory(
            settings, client, XmlCatalogSource(tmp_path, 1000, "quarantine"), resolver
        )
        with pytest.raises(MapperError):
            await factory.source(config, progress).read()
    assert not settings.catalog_staging_directory.exists() or not list(
        settings.catalog_staging_directory.iterdir()
    )
