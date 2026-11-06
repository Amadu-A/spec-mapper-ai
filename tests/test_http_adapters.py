# tests/test_http_adapters.py
"""Контрактные tests HTTP адаптеров с явным httpx MockTransport без live-pass утверждений."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from src.domain.models import MapperError
from src.infrastructure.http_clients import HttpExtractionSource, SharedEmbeddings
from src.infrastructure.qdrant import QdrantIndex


async def progress(stage):
    """Принимает checkpoint этапа для unit проверки upstream polling."""
    return None


async def test_polling_downloads_only_completed_and_sends_server_key(settings, source):
    """queued/running не скачивают result.json; готовый JSON загружается один раз с API-ключом."""
    states = iter(["queued", "running", "completed_with_warnings"])
    downloads = []

    def handler(request):
        """Фиксирует реальный HTTP контракт без использования cookie upstream."""
        assert request.headers["X-API-Key"] == "f" * 32
        assert "cookie" not in request.headers
        if request.url.path.endswith("result.json"):
            downloads.append(request.url.path)
            return httpx.Response(200, json=source)
        return httpx.Response(200, json={"status": next(states)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await HttpExtractionSource(client, settings).fetch(str(uuid4()), progress) == source
    assert len(downloads) == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "upstream_not_found"),
        (410, "upstream_expired"),
        (403, "upstream_forbidden"),
        (401, "upstream_unauthorized"),
    ],
)
async def test_upstream_errors_are_distinct(settings, status, code):
    """Сохраняет диагностируемые состояния expired/not_found/owner errors вместо пустого успеха."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        with pytest.raises(MapperError) as error:
            await HttpExtractionSource(client, settings).fetch(str(uuid4()), progress)
        assert error.value.code == code


@pytest.mark.parametrize(
    "vectors", [[], [{"index": 1, "embedding": [1, 0]}], [{"index": 0, "embedding": [0, 0]}]]
)
async def test_embedding_contract_rejects_wrong_results(settings, vectors):
    """Неверный порядок, пустой ответ и нулевой вектор не попадают в Qdrant."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": vectors}))
    ) as client:
        with pytest.raises(MapperError):
            await SharedEmbeddings(client, settings).embed(["Текст"])


async def test_qdrant_batch_and_atomic_alias(settings):
    """Закрепляет actual REST контракт query/batch и единый atomic alias update."""
    requests = []

    def handler(request):
        """Эмулирует только документированный wire contract Qdrant REST API."""
        requests.append(request)
        if request.url.path == "/aliases":
            return httpx.Response(
                200,
                json={"result": {"aliases": [{"alias_name": "catalog", "collection_name": "old"}]}},
            )
        if request.url.path.endswith("query/batch"):
            assert len(json.loads(request.content)["searches"]) == 2
            return httpx.Response(200, json={"result": [{"points": []}, {"points": []}]})
        return httpx.Response(200, json={"result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        index = QdrantIndex(client, "http://qdrant:6333", "catalog")
        assert await index.switch("new") == "old"
        actions = json.loads(requests[-1].content)["actions"]
        assert "delete_alias" in actions[0] and "create_alias" in actions[1]
        assert await index.search("old", [[1, 0], [1, 0]], 5) == [[], []]


async def test_qdrant_snapshot_clone_is_isolated_and_temporary_snapshot_removed():
    """Snapshot recovery нацелено только на новую коллекцию, временный snapshot удаляется."""
    requests = []

    def handler(request):
        """Закрепляет documented snapshot/recover REST контракт без live Qdrant."""
        requests.append(request)
        if request.url.path.endswith("/exists"):
            return httpx.Response(200, json={"result": {"exists": False}})
        if request.method == "POST":
            return httpx.Response(
                200, json={"result": {"name": "old-safe.snapshot", "checksum": "abc"}}
            )
        return httpx.Response(200, json={"result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        index = QdrantIndex(client, "http://qdrant:6333", "catalog")
        await index.clone("old", "new")
        recovery = next(request for request in requests if request.method == "PUT")
        assert recovery.url.path == "/collections/new/snapshots/recover"
        assert (
            json.loads(recovery.content)["location"]
            == "http://qdrant:6333/collections/old/snapshots/old-safe.snapshot"
        )
        assert requests[-1].method == "DELETE"
        assert requests[-1].url.path == "/collections/old/snapshots/old-safe.snapshot"
