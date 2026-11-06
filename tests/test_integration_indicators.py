# tests/test_integration_indicators.py
"""Mock upstream health и настоящая SQL фиксация только успешных inbound API обращений."""

import httpx
import pytest
from pydantic import SecretStr

from src.infrastructure.integration_status import IntegrationProbes
from src.main import create_app


@pytest.mark.parametrize(
    "key,status,expected",
    [
        ("", 404, "key_missing"),
        ("secret", 404, "available"),
        ("secret", 401, "unauthorized"),
        ("secret", 403, "unauthorized"),
        ("secret", 500, "unavailable"),
    ],
)
async def test_extracting_probe_contract_auth_and_cache(settings, key, status, expected):
    """Проверяет endpoint и право по несуществующему UUID, не читая чужие документы."""
    settings.extracting_api_key = SecretStr(key)
    requests = []

    def handler(request):
        """Эмулирует только официальный OpenAPI и безопасный запрос к UUID."""
        requests.append(request)
        if request.url.path.endswith("openapi.json"):
            return httpx.Response(200, json={"paths": {"/api/v1/extractions/{job_id}": {}}})
        assert request.url.path.endswith("00000000-0000-4000-8000-000000000000")
        assert request.headers["X-API-Key"] == key
        return httpx.Response(status, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        probe = IntegrationProbes(client, settings)
        assert (await probe.extracting())["state"] == expected
        count = len(requests)
        assert (await probe.extracting())["state"] == expected
        assert len(requests) == count


async def test_named_key_inbound_activity_no_secret_or_false_success(system):
    """Наличие ключа не означает подключение; 401/404 не фиксируются как успешная активность."""
    container, *_ = system
    container.settings.api_keys = {"telegram": SecretStr("t" * 32)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://localhost"
    ) as client:
        assert await container.sync_repository.activity("api:telegram") is None
        assert (
            await client.get("/api/v1/catalog/status", headers={"X-API-Key": "wrong"})
        ).status_code == 401
        assert await container.sync_repository.activity("api:telegram") is None
        assert (
            await client.get("/api/v1/catalog/status", headers={"X-API-Key": "t" * 32})
        ).status_code == 200
        assert await container.sync_repository.activity("api:telegram") is not None
        headers = {"X-Catalog-Key": "b" * 32}
        response = await client.get("/api/v1/catalog/sync-settings", headers=headers)
        assert response.status_code == 200 and "t" * 32 not in response.text
        assert "source_identity" not in response.text
