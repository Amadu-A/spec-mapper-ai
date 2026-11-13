# tests/test_api.py
"""API/security контракты, реальные cookie/CSRF, owner isolation, ревью и экспорты."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import httpx
from openpyxl import load_workbook

from src.main import create_app


async def test_api_contract_and_error_states(system, source):
    """Проверяет строгий вход, idempotency и запрет выдачи незавершённого результата."""
    container, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "test-key"}
        assert (await client.get("/api/v1/mappings")).status_code == 401
        assert (
            await client.post("/api/v1/mappings", headers=headers, json={"source_job_id": "bad"})
        ).status_code == 422
        created = await client.post(
            "/api/v1/mappings", headers=headers, json={"source_job_id": str(uuid4())}
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        status = await client.get(f"/api/v1/mappings/{job_id}", headers=headers)
        assert set(status.json()) == {
            "job_id",
            "kind",
            "status",
            "stage",
            "source_job_id",
            "created_at",
            "expires_at",
            "revision",
            "approved_at",
            "approved_revision",
            "unresolved_count",
            "error_code",
            "total",
            "processed",
            "counts",
            "catalog_version",
            "links",
        }
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/result", headers=headers)
        ).status_code == 409
        schema = (await client.get("/api/openapi.json")).json()
        assert "/api/v1/mappings/{job_id}/rows/{row_index}/selection" in schema["paths"]
        assert "Idempotency-Key" in str(schema["paths"]["/api/v1/mappings"]["post"])


async def test_browser_cookie_origin_csrf_and_owner_isolation(system, source):
    """Две вкладки разных сессий не читают чужие задания; web writes требуют Origin и CSRF."""
    container, *_ = system
    transport = httpx.ASGITransport(app=create_app(container))
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as first,
        httpx.AsyncClient(transport=transport, base_url="http://test") as other,
    ):
        session = await first.get("/api/v1/session")
        cookie = session.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        csrf = session.json()["csrf"]
        headers = {"Idempotency-Key": "cookie-key", "X-CSRF-Token": csrf}
        assert (
            await first.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).status_code == 403
        headers["Origin"] = "http://evil.test"
        assert (
            await first.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).status_code == 403
        headers["Origin"] = "http://test"
        created = await first.post("/api/v1/mappings/import-json", json=source, headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        await other.get("/api/v1/session")
        assert (await other.get(f"/api/v1/mappings/{job_id}")).status_code == 404
        assert (await first.get(f"/api/v1/mappings/{job_id}")).status_code == 200
        assert (
            await first.post("/api/v1/catalog/imports", headers={**headers, "X-API-Key": "a" * 32})
        ).status_code == 403


async def test_configured_public_origin_works_without_trusting_forwarded_headers(system, source):
    """HTTPS proxy использует заданный public origin; чужой forwarded origin запрещён."""
    container, *_ = system
    container.settings.app_public_base_url = "https://mapper.example.test"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://internal"
    ) as client:
        csrf = (await client.get("/api/v1/session")).json()["csrf"]
        headers = {
            "Idempotency-Key": "proxy",
            "X-CSRF-Token": csrf,
            "Origin": "https://evil.test",
            "X-Forwarded-Host": "evil.test",
            "X-Forwarded-Proto": "https",
        }
        assert (
            await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).status_code == 403
        headers["Origin"] = "https://mapper.example.test"
        assert (
            await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        ).status_code == 202


async def test_manual_export_pagination_and_formula_literals(system, source):
    """JSON/Excel сохраняют исходные строки, даже начинающиеся с =, и учитывают ручной отказ."""
    container, *_ = system
    source["items"][0]["UserComment1"] = '=HYPERLINK("https://invalid.test")'
    snapshot = deepcopy(source)
    app = create_app(container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "export-key"}
        created = await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        job_id = created.json()["job_id"]
        await container.processor.process(job_id)
        result = (
            await client.get(f"/api/v1/mappings/{job_id}/result?limit=1", headers=headers)
        ).json()
        assert result["total"] == 2 and result["filtered_total"] == 2 and len(result["items"]) == 1
        selected = await client.put(
            f"/api/v1/mappings/{job_id}/rows/0/selection",
            json={"catalog_item_guid": None, "revision": result["revision"]},
            headers=headers,
        )
        assert selected.status_code == 200
        assert selected.json()["counts"]["not_found"] == 1
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/source.json", headers=headers)
        ).json() == snapshot
        exported = (
            await client.get(f"/api/v1/mappings/{job_id}/result.json", headers=headers)
        ).json()
        assert exported["items"][0]["UserComment1"] == snapshot["items"][0]["UserComment1"]
        assert exported["items"][0]["onec"] is None
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/result.xlsx", headers=headers)
        ).status_code == 409
        approval = await client.post(
            f"/api/v1/mappings/{job_id}/approve",
            json={"revision": selected.json()["revision"]},
            headers=headers,
        )
        assert approval.status_code == 200
        excel = await client.get(f"/api/v1/mappings/{job_id}/result.xlsx", headers=headers)
        workbook = load_workbook(BytesIO(excel.content), data_only=False)
        sheet = workbook.active
        assert sheet.max_row == 3
        assert sheet["Q2"].value == snapshot["items"][0]["UserComment1"]
        assert sheet["Q2"].data_type == "s"
        assert sheet["E2"].value == " 2,50 "


async def test_upload_limit_and_security_headers(system, source):
    """Фактический body ограничивается до parsing; ответы не кэшируются и
    запрещают inline scripts.
    """
    container, *_ = system
    container.settings.max_result_bytes = 1024
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/mappings/import-json", content=b"x" * 1025)
        assert response.status_code == 413
        live = await client.get("/health/live")
        assert live.json() == {"live": True}
        assert "script-src 'self'" in live.headers["content-security-policy"]
        page = await client.get("/")
        assert page.status_code == 200
        assert 'type="module"' in page.text
        assert "onclick=" not in page.text
        assert "a" * 32 not in page.text
