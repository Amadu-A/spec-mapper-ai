# tests/test_approval.py
"""Контракты 1С/бота: import → review/null → approve → immutable JSON, owner и legacy isolation."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from src.domain.models import MapperError
from src.main import create_app
from src.presentation.schemas import ApprovedExport


async def test_onec_chain_named_owners_source_link_and_immutable_approval(system, source):
    """Выполняет полный HTTP-контракт 1С и проверяет невозможность чтения другим клиентом."""
    container, *_ = system
    container.settings.api_keys = {"onec": SecretStr("o" * 32), "telegram": SecretStr("t" * 32)}
    original = deepcopy(source)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        source_id = str(uuid4())
        headers = {
            "X-API-Key": "o" * 32,
            "Idempotency-Key": "onec-create",
            "X-Source-Job-ID": source_id,
        }
        created = await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert created.json()["source_job_id"] == source_id
        assert created.json()["links"]["status"] == f"/api/v1/mappings/{job_id}"
        assert (
            await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).json()["job_id"] == job_id
        changed = await client.post(
            "/api/v1/mappings/import-json",
            json=source,
            headers={**headers, "X-Source-Job-ID": str(uuid4())},
        )
        assert changed.status_code == 409
        await container.processor.process(job_id)
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/approved.json", headers=headers)
        ).status_code == 409
        status = (await client.get(f"/api/v1/mappings/{job_id}", headers=headers)).json()
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/candidates?row_index=0", headers=headers)
        ).json()["candidates"]
        chosen = await client.put(
            f"/api/v1/mappings/{job_id}/rows/0/selection",
            headers=headers,
            json={"catalog_item_guid": None, "revision": status["revision"]},
        )
        assert chosen.status_code == 200
        revision = chosen.json()["revision"]
        stale = await client.post(
            f"/api/v1/mappings/{job_id}/approve", headers=headers, json={"revision": revision - 1}
        )
        assert stale.status_code == 409
        approved = await client.post(
            f"/api/v1/mappings/{job_id}/approve", headers=headers, json={"revision": revision}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["expires_at"] == status["expires_at"]
        snapshot = (
            await client.get(f"/api/v1/mappings/{job_id}/approved.json", headers=headers)
        ).json()
        assert snapshot["items"][0]["onec"] is None
        assert snapshot["items"][1]["onec"]["code"] == "00-00001"
        assert snapshot["items"][1]["onec"]["onec_guid"] is None
        assert [row["index"] for row in snapshot["items"]] == [0, 1]
        assert snapshot["approval"]["owner"] == "api:onec"
        assert ApprovedExport.model_validate(snapshot).items[0].onec is None
        assert (
            await client.post(
                f"/api/v1/mappings/{job_id}/approve", headers=headers, json={"revision": revision}
            )
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/approved.json", headers=headers)
        ).json() == snapshot
        assert (
            await client.put(
                f"/api/v1/mappings/{job_id}/rows/0/selection",
                headers=headers,
                json={"catalog_item_guid": None, "revision": revision},
            )
        ).status_code == 409
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/source.json", headers=headers)
        ).json() == original
        other = {"X-API-Key": "t" * 32, "Idempotency-Key": "other"}
        for suffix in (
            "",
            "/result",
            "/source.json",
            "/approved.json",
            "/result.json",
            "/result.xlsx",
        ):
            assert (
                await client.get(f"/api/v1/mappings/{job_id}" + suffix, headers=other)
            ).status_code == 404
        assert (
            await client.post(
                f"/api/v1/mappings/{job_id}/approve", headers=other, json={"revision": revision}
            )
        ).status_code == 404
        container.settings.api_keys["onec"] = SecretStr("n" * 32)
        assert (
            await client.get(f"/api/v1/mappings/{job_id}", headers={"X-API-Key": "n" * 32})
        ).status_code == 200
        assert (await client.get(f"/api/v1/mappings/{job_id}", headers=headers)).status_code == 401


async def test_unresolved_null_approval_and_ttl(system, source):
    """Общее утверждение подтверждает пустой Top-K, сохраняет null и исходный TTL."""
    container, *_ = system
    container.index.collections[container.index.alias] = {}
    job = await container.mappings.import_document("owner", "empty-hits", source)
    await container.processor.process(job["id"])
    ready = await container.jobs.get(job["id"])
    done = await container.mappings.approve("owner", job["id"], ready["revision"], "a")
    assert done["expires_at"] == ready["expires_at"]
    assert done["revision"] == ready["revision"]
    assert all(row["confirmed"] for row in done["data"]["rows"])
    assert all(row["status"] == "not_found" for row in done["data"]["rows"])
    assert all(
        row["onec"] is None for row in (await container.jobs.approved(job["id"], "owner"))["items"]
    )
    container.jobs.clock = lambda: done["expires_at"] + 1
    with pytest.raises(MapperError) as error:
        await container.jobs.approved(job["id"], "owner")
    assert error.value.status == 410
    await container.jobs.cleanup()
    assert (await container.catalog.status())["ready"]


async def test_legacy_key_keeps_same_owner_after_named_keys_added(system, source):
    """Существующие legacy jobs остаются доступны по API_KEY после появления новых интеграций."""
    container, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "old"}
        job = (
            await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).json()
        container.settings.api_keys = {"onec": SecretStr("o" * 32)}
        assert (
            await client.get(f"/api/v1/mappings/{job['job_id']}", headers=headers)
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/mappings/{job['job_id']}", headers={"X-API-Key": "o" * 32})
        ).status_code == 404


async def test_local_docs_and_openapi_security_have_no_keys_or_cdn(system):
    """Страница /docs использует локальные ресурсы и объявляет реальные security/header схемы."""
    container, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        docs = await client.get("/docs")
        assert docs.status_code == 200 and "https://" not in docs.text
        assert "a" * 32 not in docs.text
        schema = (await client.get("/api/openapi.json")).json()
        assert schema["components"]["securitySchemes"]["IntegrationKey"]["name"] == "X-API-Key"
        assert "/api/v1/mappings/{job_id}/approve" in schema["paths"]
        assert "X-Source-Job-ID" in str(schema["paths"]["/api/v1/mappings/import-json"])
        final_schema = schema["paths"]["/api/v1/mappings/{job_id}/approved.json"]["get"]
        assert final_schema["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApprovedExport"
        }
