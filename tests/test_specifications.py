# tests/test_specifications.py
"""Единый PDF-контракт: durable upload, повторы, изоляция, restart, ревью и экспорт.

Использует реальные SQLite-репозитории и worker; HTTP извлечения и модели подменены.
Результаты этих тестов не подтверждают PostgreSQL, AMQP или реальные модели.
"""

from __future__ import annotations

import asyncio
import importlib.util
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from openpyxl import load_workbook
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select

from src.domain.models import MapperError
from src.infrastructure.database import JobRow, SpecificationUploadRow
from src.infrastructure.http_clients import HttpExtractionSource
from src.main import create_app

PDF = b"%PDF-1.7\nexample-pdf-for-mocked-upstream"
HEADERS = {"X-API-Key": "a" * 32, "Idempotency-Key": "upload-one"}


async def create(client, **kwargs):
    """Отправляет multipart PDF через настоящий API, позволяя изменять параметры сценария."""
    return await client.post(
        "/api/v1/specifications",
        headers=kwargs.pop("headers", HEADERS),
        files={"file": ("sample.pdf", kwargs.pop("content", PDF), "application/pdf")},
        data=kwargs.pop("data", {"pages": "1", "project_code": "TEST-01"}),
        **kwargs,
    )


async def test_complete_specification_flow(system, source):
    """Клиент использует один ID; worker передаёт PDF/параметры и фиксирует финальное ревью."""
    c, *_ = system
    document_id, extraction_id = str(uuid4()), str(uuid4())
    calls = []

    async def upstream(request):
        """Проверяет ключ server-to-server и выдаёт только финальные исходные строки."""
        calls.append(request)
        assert request.headers["X-API-Key"] == "f" * 32
        path = request.url.path
        if path.endswith("/documents"):
            assert PDF in request.content
            return httpx.Response(201, json={"id": document_id})
        if request.method == "POST":
            assert document_id.encode() in request.content
            assert b"TEST-01" in request.content
            return httpx.Response(202, json={"job_id": extraction_id})
        if path.endswith("result.json"):
            return httpx.Response(200, json=source)
        return httpx.Response(200, json={"status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client:
        c.processor.extraction = HttpExtractionSource(upstream_client, c.settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
        ) as client:
            response = await create(client)
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            root = f"/api/v1/specifications/{job_id}"
            assert response.json()["status"] == "queued"
            assert "source_job_id" not in response.json()
            assert all("/specifications/" in link for link in response.json()["links"].values())
            assert (await create(client)).json()["job_id"] == job_id
            assert (await create(client, content=PDF + b"changed")).status_code == 409
            assert (await client.get(root + "/rows", headers=HEADERS)).status_code == 409
            assert (await client.get(root + "/result.json", headers=HEADERS)).status_code == 409
            await c.processor.process(job_id)
            page = (await client.get(root + "/rows", headers=HEADERS)).json()
            assert page["status"] == "awaiting_review", page
            assert "source_job_id" not in page and "kind" not in page and "stage" not in page
            assert len(page["items"]) == 2
            assert page["items"][0]["source"]["Quantity"] == " 2,50 "
            assert (
                await client.get(root + "/candidates?row_index=0", headers=HEADERS)
            ).status_code == 200
            selected = await client.put(
                root + "/rows/0/selection",
                headers=HEADERS,
                json={"catalog_item_guid": None, "revision": page["revision"]},
            )
            assert selected.status_code == 200
            revision = selected.json()["revision"]
            stale = await client.post(
                root + "/approve", headers=HEADERS, json={"revision": revision - 1}
            )
            assert stale.status_code == 409
            approved = await client.post(
                root + "/approve", headers=HEADERS, json={"revision": revision}
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"
            assert (
                await client.post(root + "/approve", headers=HEADERS, json={"revision": revision})
            ).status_code == 200
            snapshot = (await client.get(root + "/result.json", headers=HEADERS)).json()
            assert "source_job_id" not in snapshot
            assert "owner" not in snapshot["approval"]
            assert snapshot["items"][0]["onec"] is None
            excel = await client.get(root + "/result.xlsx", headers=HEADERS)
            assert excel.status_code == 200
            workbook = load_workbook(BytesIO(excel.content))
            assert workbook.active["C2"].value == source["items"][0]["Caption"]
            assert (
                await client.put(
                    root + "/rows/0/selection",
                    headers=HEADERS,
                    json={"catalog_item_guid": None, "revision": revision},
                )
            ).status_code == 409
            await c.processor.process(job_id)
            assert len(calls) == 4


async def test_isolation_validation_and_expiry(system):
    """Чужой owner, legacy ID, невалидный PDF и истёкший upload не дают доступа к данным."""
    c, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        assert (await create(client, headers={})).status_code == 401
        assert (await create(client, headers={"X-API-Key": "a" * 32})).status_code == 422
        assert (await create(client, content=b"not PDF")).status_code == 422
        assert (await create(client, data={"pages": "", "project_code": "p"})).status_code == 422
        assert (await create(client, data={"pages": "1", "project_code": " "})).status_code == 422
        c.settings.max_pdf_bytes = 1024
        c.specifications.max_upload_bytes = 1024
        assert (await create(client, content=PDF + b"x" * 1024)).status_code == 413
        job_id = (await create(client)).json()["job_id"]
        root = f"/api/v1/specifications/{job_id}"
        c.settings.api_keys = {"other": SecretStr("z" * 32)}
        for path in [root, root + "/rows", root + "/result.json", root + "/result.xlsx"]:
            assert (await client.get(path, headers={"X-API-Key": "z" * 32})).status_code == 404
        legacy = await c.mappings.submit("legacy", "key", str(uuid4()))
        with pytest.raises(MapperError, match="Спецификация"):
            await c.specifications.get("legacy", legacy["id"])
        job = await c.jobs.get(job_id)
        c.jobs.clock = lambda: job["expires_at"] + 1
        assert (await client.get(root, headers=HEADERS)).status_code == 410
        await c.jobs.cleanup()
        async with c.jobs.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(SpecificationUploadRow)) == 0
            )
            assert await session.get(JobRow, job_id) is None


async def test_retry_resumes_document_and_idempotent_start(system, monkeypatch):
    """После сетевого сбоя запуска worker переиспользует документ и ключ общего задания."""
    c, *_ = system
    document_id, extraction_id = str(uuid4()), str(uuid4())
    uploads, starts = [], []

    async def upstream(request):
        """Моделирует принятый запуск с потерянным ответом, затем успешный повтор."""
        if request.url.path.endswith("/documents"):
            uploads.append(request)
            return httpx.Response(201, json={"id": document_id})
        starts.append(request)
        if len(starts) == 1:
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(202, json={"job_id": extraction_id})

    job = await c.specifications.submit("test", "key", PDF, "a.pdf", "1", "P")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        extraction = HttpExtractionSource(client, c.settings)
        claimed = await c.jobs.claim(job["id"])

        async def checkpoint(**changes):
            """Фиксирует настоящий fenced checkpoint до моделируемого перезапуска worker."""
            assert await c.jobs.checkpoint(job["id"], claimed["lease_token"], **changes)

        async def progress(stage):
            """Обновляет технический этап без подмены долговечного payload."""
            await checkpoint(state=stage, stage=stage)

        c.processor.extraction = extraction
        with pytest.raises(MapperError, match="недоступен"):
            await c.processor._extract_pdf(claimed, checkpoint, progress)
        resumed = await c.jobs.get(job["id"])
        assert resumed["data"]["upstream_document_id"] == document_id
        await c.processor._extract_pdf(resumed, checkpoint, progress)
        assert len(uploads) == 1 and len(starts) == 2
        assert (
            starts[0].headers["Idempotency-Key"]
            == starts[1].headers["Idempotency-Key"]
            == job["id"]
        )
        assert (await c.jobs.get(job["id"]))["data"]["source_job_id"] == extraction_id


@pytest.mark.parametrize("code", [401, 403, 404, 409, 410, 413, 422, 429, 500])
async def test_upstream_errors_are_bounded_and_safe(settings, code):
    """Ответы внешнего сервиса становятся явными ошибками без раскрытия upstream payload."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(code, text="private"))
    ) as client:
        with pytest.raises(MapperError) as error:
            await HttpExtractionSource(client, settings).upload(PDF, "test.pdf")
        assert error.value.code.startswith("upstream_")
        assert "private" not in error.value.message


async def test_concurrent_upload_is_one_durable_job(system):
    """Конкурирующие повторы одного PDF создают один job и один blob в SQL."""
    c, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        replies = await asyncio.gather(create(client), create(client))
        assert all(r.status_code == 202 for r in replies)
        assert replies[0].json()["job_id"] == replies[1].json()["job_id"]
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"]["/api/v1/specifications"]["post"]
        assert "multipart/form-data" in operation["requestBody"]["content"]
        assert (
            "source_job_id"
            not in schema["components"]["schemas"]["SpecificationStatus"]["properties"]
        )
    async with c.jobs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SpecificationUploadRow)) == 1
        assert await session.scalar(select(func.count()).select_from(JobRow)) == 1


def test_upload_migration_preserves_legacy_schema(tmp_path):
    """Настоящая additive revision создаёт blob-таблицу поверх прежних таблиц без потери job."""
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    with engine.begin() as connection:
        from src.infrastructure.database import Base

        Base.metadata.create_all(connection)
        SpecificationUploadRow.__table__.drop(connection)
        connection.exec_driver_sql(
            "INSERT INTO mapping_jobs "
            "(id, owner, idempotency_hash, request_hash, kind, state, stage, created_at, "
            "expires_at, attempts, revision, data) VALUES "
            "('legacy', 'owner', 'key', 'hash', 'mapping', 'approved', "
            "'approved', 0, 9999999999, 1, 3, '{}')"
        )
        path = Path("alembic/versions/0005_specification_uploads.py")
        spec = importlib.util.spec_from_file_location("migration_uploads", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "specification_uploads" in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("SELECT revision FROM mapping_jobs").scalar() == 3
        fk = inspect(connection).get_foreign_keys("specification_uploads")[0]
        assert fk["referred_table"] == "mapping_jobs"
        assert fk["options"]["ondelete"] == "CASCADE"
    engine.dispose()
