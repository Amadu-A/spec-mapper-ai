# tests/test_invoices.py
"""Invoice unit/functional/security/SQL integration: реальные адаптеры, mock внешних систем.

Эти проверки не подтверждают доступность VLM, PostgreSQL, AMQP и Qdrant на сервере.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import httpx
import pymupdf
import pytest
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import func, select

from src.application.invoices.service import selected_pages
from src.application.mappings import MappingUseCases
from src.domain.invoices.adapter import invoice_items, matching_source
from src.domain.invoices.models import validate_invoice
from src.domain.invoices.warnings import approval_context, financial_warnings
from src.domain.models import MapperError, digest
from src.infrastructure.database import InvoiceUploadRow, JobRow
from src.infrastructure.invoices.extracting import HttpInvoiceExtraction
from src.main import create_app
from src.presentation.invoice_schemas import InvoiceMapping

AUTH = {"X-API-Key": "a" * 32, "Idempotency-Key": "invoice-one"}


@pytest.fixture
def invoice_source():
    """Синтетический полный v1: дубли, услуга, платёж и неизвестная строка без личных данных."""
    item = {
        "row_number": 1,
        "caption": "Клапан балансировочный DN50 PN16",
        "units": "шт",
        "brand": "ROSMA",
        "manufacturer": None,
        "article": "000123",
        "article_candidate": "000999",
        "supplier_code": "000001",
        "product_reference_raw": None,
        "row_kind": "product",
        "quantity": "2.00",
        "unit_price_without_vat": "00100.00",
        "unit_price_with_vat": None,
        "line_total_without_vat": "200.00",
        "vat_rate": "22",
        "vat_amount": "44.00",
        "line_total_with_vat": "244.00",
        "source_pages": [1],
        "vat_rate_source": "row",
        "field_origin": {"quantity": "source"},
        "derived_fields": [],
        "technical_parameters_pages": [],
        "requires_manual_review": False,
    }
    return {
        "schema_version": "invoice_extraction.v1",
        "files": [
            {
                "file_index": 0,
                "source_file": "invoice.pdf",
                "source_sha256": "1" * 64,
                "page_count": 2,
                "documents": [
                    {
                        "document_type": "commercial_offer",
                        "number": "0000123",
                        "date": "2026-11-23",
                        "supplier": {
                            "name": "Тестовый поставщик",
                            "inn": "0000000001",
                            "kpp": None,
                        },
                        "buyer": {
                            "name": "Тестовый покупатель",
                            "inn": "0000000002",
                            "kpp": "000000001",
                        },
                        "currency": "RUB",
                        "items": [
                            deepcopy(item),
                            deepcopy(item),
                            {**deepcopy(item), "row_number": 3, "row_kind": "service"},
                            {
                                **deepcopy(item),
                                "row_number": 4,
                                "row_kind": "final_payment",
                                "quantity": None,
                            },
                            {**deepcopy(item), "row_number": 5, "row_kind": "other"},
                        ],
                        "totals": {
                            "without_vat": "1000.00",
                            "vat": "220.00",
                            "with_vat": "1220.00",
                            "adjustments": [],
                        },
                        "validation": {
                            "status": "failed",
                            "checks": [{"check": "sum", "status": "failed", "difference": "1.00"}],
                            "warnings": ["Синтетическое расхождение"],
                        },
                        "payment_terms": "Оплата после согласования",
                        "notes": ["Исходный текст"],
                        "source_pages": [1],
                        "attached_pages": [2],
                    }
                ],
            }
        ],
        "summary": {"file_count": 1, "document_count": 1, "item_count": 5},
    }


@pytest.fixture
async def invoice_system(system):
    """Готовит шесть позиций настоящего SQL-каталога и общий fake embedding/index."""
    c, item, sync, source, _ = system
    source.items = []
    for index in range(6):
        identifier = str(uuid4())
        source.items.append(
            replace(
                item,
                guid=identifier,
                onec_guid=identifier,
                code=f"00000{index}",
                brand="ROSMA",
                article="000123",
            )
        )
    source.source_hash = "invoice-catalog"
    await sync.execute(lambda stage, **details: _nothing())
    yield c, source.items


async def _nothing():
    """Принимает этап standalone импорта, не подменяя SQL/worker."""
    return None


def pdf_bytes(pages=2):
    """Создаёт настоящий небольшой PDF без приватных документов."""
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page()
        return document.tobytes()


def image_bytes(kind="PNG"):
    """Создаёт настоящее валидное изображение для MIME/preview transport тестов."""
    output = BytesIO()
    Image.new("RGB", (10, 10), color="white").save(output, format=kind)
    return output.getvalue()


async def imported(client, source, key="invoice-one"):
    """Принимает полный JSON через публичный профиль, проверяя durable 202."""
    response = await client.post(
        "/api/v1/invoices/import-json", headers={**AUTH, "Idempotency-Key": key}, json=source
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def test_lossless_validation_and_derived_fields(invoice_source):
    """Не меняет финансы/дубли; неподтверждённый артикул и supplier_code не входят в engine."""
    before = deepcopy(invoice_source)
    assert validate_invoice(invoice_source) is invoice_source
    entries = list(invoice_items(invoice_source))
    assert len(entries) == 5
    assert entries[0][2] == entries[1][2]
    assert entries[0][3]["row_number"] == entries[1][3]["row_number"] == 1
    derived = matching_source(entries[0][2])
    assert derived["Article"] == "000123"
    changed = {
        **entries[0][2],
        "article": None,
        "brand": None,
        "manufacturer": "ROSMA",
        "units": None,
    }
    assert matching_source(changed)["Article"] == ""
    assert matching_source(changed)["Brand"] == "ROSMA"
    assert matching_source(changed)["Units"] == ""
    assert invoice_source == before
    changed = deepcopy(invoice_source)
    changed["files"][0]["documents"][0]["notes"] = "Исторический корпус"
    changed["files"][0]["documents"][0]["items"][0]["future_optional"] = {"text": "0001"}
    assert validate_invoice(changed) == changed
    assert digest(changed) != digest(before)


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "money_float",
        "bool_index",
        "summary",
        "pages",
        "reserved",
        "invalid_date",
        "notes_limit",
        "missing_caption",
    ],
)
def test_invalid_invoice_source(invoice_source, mutation):
    """Версия, строгие decimal-строки, адресация и защищённое пространство полей обязательны."""
    source = deepcopy(invoice_source)
    item = source["files"][0]["documents"][0]["items"][0]
    if mutation == "version":
        source["schema_version"] = "invoice_extraction.v2"
    elif mutation == "money_float":
        item["quantity"] = 2.0
    elif mutation == "bool_index":
        source["files"][0]["file_index"] = False
    elif mutation == "summary":
        source["summary"]["item_count"] -= 1
    elif mutation == "pages":
        item["source_pages"] = [3]
    elif mutation == "reserved":
        item["onec"] = {"guid": str(uuid4())}
    elif mutation == "invalid_date":
        source["files"][0]["documents"][0]["date"] = "2026-02-30"
    elif mutation == "notes_limit":
        source["files"][0]["documents"][0]["notes"] = ["Примечание"] * 201
    else:
        del item["caption"]
    with pytest.raises(MapperError) as error:
        validate_invoice(source)
    assert error.value.code == "invalid_invoice_source"


def test_financial_acknowledgment(invoice_source):
    """Общий approve не исправляет failed/неполные финансы и требует явное согласие."""
    original = deepcopy(invoice_source)
    assert financial_warnings(invoice_source)[0]["document_id"] == "0:0"
    for values, code in [
        ([], "financial_acknowledgment_required"),
        (["1:0"], "invalid_warning_acknowledgment"),
        (["0:0", "0:0"], "invalid_warning_acknowledgment"),
    ]:
        with pytest.raises(MapperError) as error:
            approval_context(invoice_source, values)
        assert error.value.code == code
    assert (
        approval_context(invoice_source, ["0:0"])["financial_warnings"][0]["validation_status"]
        == "failed"
    )
    assert invoice_source == original


@pytest.mark.parametrize("value", ["0", "3", "2-1", "1,,2", "1-a", "1-999999999999999999"])
def test_page_selection_limits(value):
    """Неверный диапазон не выделяет неограниченный range и не достигает 8083."""
    with pytest.raises(MapperError):
        selected_pages(value, 2)
    assert selected_pages("1-2,2", 2) == [1, 2]


async def test_complete_invoice_flow(invoice_system, invoice_source):
    """Один job, Top-5, оплата без embedding, CAS/null/finance guard, immutable JSON/Excel."""
    c, catalog = invoice_system
    assert type(c.invoices.mappings) is MappingUseCases
    calls = c.embeddings.calls
    original = deepcopy(invoice_source)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        identifier = await imported(client, invoice_source)
        root = "/api/v1/invoices/" + identifier
        assert (await imported(client, invoice_source)) == identifier
        changed = deepcopy(invoice_source)
        changed["files"][0]["documents"][0]["totals"]["vat"] = "221.00"
        assert (
            await client.post("/api/v1/invoices/import-json", headers=AUTH, json=changed)
        ).status_code == 409
        for suffix in ["/rows", "/result.json", "/result.xlsx"]:
            assert (await client.get(root + suffix, headers=AUTH)).status_code == 409
        await c.processor.process(identifier)
        assert c.embeddings.calls - calls == 3
        page = (await client.get(root + "/rows", headers=AUTH)).json()
        assert page["processed"] == page["total"] == 5
        assert page["financial_warning_count"] == 1
        assert len(page["items"][0]["candidates"]) == 5
        assert page["items"][2]["status"] == "review"
        assert page["items"][3]["status"] == "not_applicable"
        assert page["items"][3]["candidates"] == []
        assert page["items"][4]["selection"] is None
        assert (await client.get(root + "/source.json", headers=AUTH)).json() == original
        assert "source_job_id" not in (await client.get(root, headers=AUTH)).json()
        before_calls = c.embeddings.calls
        assert (await client.get(root + "/candidates?row_index=0", headers=AUTH)).status_code == 200
        selected = await client.put(
            root + "/rows/0/selection",
            headers=AUTH,
            json={"catalog_item_guid": catalog[0].guid, "revision": page["revision"]},
        )
        assert selected.status_code == 200, selected.text
        revision = selected.json()["revision"]
        assert (
            await client.delete(
                root + "/rows/1/selection", headers=AUTH, params={"revision": revision - 1}
            )
        ).status_code == 409
        cleared = await client.delete(
            root + "/rows/1/selection", headers=AUTH, params={"revision": revision}
        )
        revision = cleared.json()["revision"]
        assert (
            await client.put(
                root + "/rows/3/selection",
                headers=AUTH,
                json={"catalog_item_guid": catalog[0].guid, "revision": revision},
            )
        ).status_code == 409
        assert (
            await client.post(root + "/approve", headers=AUTH, json={"revision": revision})
        ).status_code == 409
        approved = await client.post(
            root + "/approve",
            headers=AUTH,
            json={"revision": revision, "acknowledged_document_warnings": ["0:0"]},
        )
        assert approved.status_code == 200, approved.text
        snapshot = (await client.get(root + "/result.json", headers=AUTH)).json()
        InvoiceMapping.model_validate(snapshot)
        doc = snapshot["files"][0]["documents"][0]
        assert doc["document_type"] == "commercial_offer"
        assert doc["validation"] == original["files"][0]["documents"][0]["validation"]
        assert snapshot["approval"]["acknowledged_document_warnings"] == ["0:0"]
        assert doc["items"][0]["onec"]["onec_guid"] == catalog[0].onec_guid
        assert doc["items"][1]["onec"] is None
        assert doc["items"][3]["matching"]["status"] == "not_applicable"
        for saved, source in zip(
            doc["items"], original["files"][0]["documents"][0]["items"], strict=True
        ):
            assert {k: v for k, v in saved.items() if k not in {"onec", "matching"}} == source
        assert (await client.get(root + "/source.json", headers=AUTH)).json() == original
        assert (
            await client.put(
                root + "/rows/0/selection",
                headers=AUTH,
                json={"catalog_item_guid": None, "revision": revision},
            )
        ).status_code == 409
        assert (
            await client.post(root + "/approve", headers=AUTH, json={"revision": revision})
        ).status_code == 200
        assert (await client.get(root + "/result.json", headers=AUTH)).json() == snapshot
        xlsx = await client.get(root + "/result.xlsx", headers=AUTH)
        assert xlsx.status_code == 200
        book = load_workbook(BytesIO(xlsx.content))
        assert book.sheetnames == ["Документы", "Позиции", "Предупреждения"]
        assert book["Позиции"].max_row == 6
        assert book["Позиции"]["N2"].value == "00100.00"
        assert c.embeddings.calls == before_calls
        assert all(not key.startswith("upstream") for key in snapshot)


async def test_profile_owner_ttl_and_csrf(invoice_system, invoice_source, source):
    """Профили имеют независимые ключи; чужое 404, своё 410, cookies требуют CSRF/Origin."""
    c, _ = invoice_system
    app = create_app(c)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/api/v1/invoices/import-json",
                json=invoice_source,
                headers={"Idempotency-Key": "key"},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/invoices/import-json", json=invoice_source, headers={"X-API-Key": "a" * 32}
            )
        ).status_code == 422
        identifier = await imported(client, invoice_source)
        root = "/api/v1/invoices/" + identifier
        legacy = await client.post("/api/v1/mappings/import-json", headers=AUTH, json=source)
        assert legacy.status_code == 202
        assert legacy.json()["job_id"] != identifier
        for suffix in [
            "",
            "/rows",
            "/source.json",
            "/result.json",
            "/result.xlsx",
            "/candidates?row_index=0",
        ]:
            assert (
                await client.get(root + suffix, headers={"X-API-Key": "wrong"})
            ).status_code == 401
            assert (await client.get(root + suffix, headers={})).status_code == 401
        for prefix in ["/api/v1/mappings/", "/api/v1/specifications/"]:
            for suffix in ["", "/result.json", "/result.xlsx"]:
                assert (
                    await client.get(prefix + identifier + suffix, headers=AUTH)
                ).status_code == 404
        assert all(
            row["job_id"] != identifier
            for row in (await client.get("/api/v1/mappings", headers=AUTH)).json()
        )
        session = await client.get("/api/v1/session")
        assert (await client.get(root)).status_code == 404
        assert (
            await client.post(
                "/api/v1/invoices/import-json",
                headers={"Idempotency-Key": "web"},
                json=invoice_source,
            )
        ).status_code == 403
        web = await client.post(
            "/api/v1/invoices/import-json",
            headers={
                "Idempotency-Key": "web",
                "Origin": "http://test",
                "X-CSRF-Token": session.json()["csrf"],
            },
            json=invoice_source,
        )
        assert web.status_code == 202
        assert (await client.get(web.json()["links"]["status"], headers=AUTH)).status_code == 404
        job = await c.jobs.get(identifier)
        c.jobs.clock = lambda: job["expires_at"] + 1
        assert (await client.get(root, headers=AUTH)).status_code == 410
        await c.jobs.cleanup()
        assert (await client.get(root, headers=AUTH)).status_code == 404


async def test_multipart_restart_and_preview(invoice_system, invoice_source, monkeypatch):
    """Потерянный POST повторяется с тем же ключом, durable upstream ID исключает повтор запуска."""
    c, _ = invoice_system
    pdf, png = pdf_bytes(), image_bytes()
    calls = []
    identifier = str(uuid4())
    result = deepcopy(invoice_source)
    result["files"].append(deepcopy(result["files"][0]))
    for index, (file, data) in enumerate(zip(result["files"], [pdf, png], strict=True)):
        file.update(
            file_index=index,
            source_sha256=hashlib.sha256(data).hexdigest(),
            page_count=2 if index == 0 else 1,
        )
        if index:
            file["documents"][0]["attached_pages"] = []
    result["summary"] = {"file_count": 2, "document_count": 2, "item_count": 10}

    async def upstream(request):
        """Реальный HTTP client с contract mock; VLM в этом тесте не вызывается."""
        calls.append(request)
        assert request.headers["X-API-Key"] == "f" * 32
        if request.method == "POST":
            assert pdf in request.content and png in request.content
            assert request.headers["Idempotency-Key"] == mapper_id
            return httpx.Response(202, json={"job_id": identifier})
        if request.url.path.endswith("preview"):
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        if request.url.path.endswith("result.json"):
            return httpx.Response(200, json=result)
        return httpx.Response(200, json={"status": "completed_with_warnings"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as external:
        extraction = HttpInvoiceExtraction(external, c.settings)
        c.processor.invoice_extraction = c.invoices.extraction = extraction
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
        ) as client:
            files = [
                ("files", ("one.pdf", pdf, "application/pdf")),
                ("files", ("two.png", png, "image/png")),
            ]
            response = await client.post(
                "/api/v1/invoices",
                headers=AUTH,
                files=files,
                data={"page_selection": '{"0":"1-2","1":"1"}'},
            )
            assert response.status_code == 202, response.text
            mapper_id = response.json()["job_id"]
            root = "/api/v1/invoices/" + mapper_id
            assert calls == []
            assert len(await c.jobs.invoice_files(mapper_id)) == 2
            claimed = await c.jobs.claim(mapper_id)

            async def checkpoint(**changes):
                """Фиксирует настоящий lease перед имитацией рестарта worker."""
                assert await c.jobs.checkpoint(mapper_id, claimed["lease_token"], **changes)

            async def progress(stage, **details):
                """Принимает диагностический этап без воздействия на источник."""
                return None

            original_fetch = extraction.fetch

            async def interrupted(*args):
                """Останавливает обработку уже после сохранённого upstream ID."""
                raise MapperError("upstream_timeout", "Тестовый отказ", 503)

            monkeypatch.setattr(extraction, "fetch", interrupted)
            with pytest.raises(MapperError):
                await c.processor._extract_invoice(claimed, checkpoint, progress)
            await c.jobs.checkpoint(mapper_id, claimed["lease_token"], state="queued")
            monkeypatch.setattr(extraction, "fetch", original_fetch)
            await c.processor.process(mapper_id)
            state = (await client.get(root, headers=AUTH)).json()
            assert state["status"] == "awaiting_review", state
            assert state["upstream_warning"] is True
            assert sum(request.method == "POST" for request in calls) == 1
            preview = await client.get(root + "/files/1/pages/1/preview", headers=AUTH)
            assert preview.status_code == 200 and preview.content == png
            assert (
                await client.get(root + "/files/2/pages/1/preview", headers=AUTH)
            ).status_code == 404
            assert (
                await client.post(
                    "/api/v1/invoices",
                    headers=AUTH,
                    files=files,
                    data={"page_selection": '{"0":"1-2","1":"1"}'},
                )
            ).json()["job_id"] == mapper_id
            job = await c.jobs.get(mapper_id)
            c.jobs.clock = lambda: job["expires_at"] + 1
            await c.jobs.cleanup()
            async with c.jobs.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(InvoiceUploadRow)) == 0


@pytest.mark.parametrize(
    "case, expected",
    [
        ("mime", 415),
        ("corrupt", 422),
        ("empty", 422),
        ("pages", 422),
        ("selection", 422),
        ("count", 413),
        ("size", 413),
    ],
)
async def test_upload_rejection(invoice_system, case, expected):
    """Плохой пакет не создаёт ни одного job/blob и не достигает upstream."""
    c, _ = invoice_system
    data = pdf_bytes()
    files = [("files", ("one.pdf", data, "application/pdf"))]
    selection = "{}"
    if case == "mime":
        files = [("files", ("one.pdf", data, "image/png"))]
    elif case == "corrupt":
        files = [("files", ("one.pdf", b"%PDF-1.7\ncorrupt", "application/pdf"))]
    elif case == "empty":
        files = [("files", ("one.pdf", b"", "application/pdf"))]
    elif case == "pages":
        selection = '{"0":"3"}'
    elif case == "selection":
        selection = '{"1":"1"}'
    elif case == "count":
        files = files * 11
    else:
        c.settings.invoice_max_file_bytes = 1024
        files = [("files", ("one.pdf", data + b"x" * 1024, "application/pdf"))]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/invoices", files=files, headers=AUTH, data={"page_selection": selection}
        )
        assert response.status_code == expected, response.text
    async with c.jobs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InvoiceUploadRow)) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(JobRow).where(JobRow.kind == "invoice")
            )
            == 0
        )


async def test_openapi_invoice_contract(invoice_system):
    """Полный namespace и versioned DTO видимы в локальной схеме; profile независим от legacy."""
    c, _ = invoice_system
    schema = create_app(c).openapi()
    paths = schema["paths"]
    root = "/api/v1/invoices/{job_id}"
    for suffix in [
        "",
        "/rows",
        "/candidates",
        "/rows/{row_index}/selection",
        "/approve",
        "/source.json",
        "/draft.json",
        "/result.json",
        "/result.xlsx",
        "/files/{file_index}/pages/{page}/preview",
    ]:
        assert root + suffix in paths
    assert "delete" in paths[root + "/rows/{row_index}/selection"]
    assert paths["/api/v1/invoices"]["post"]["responses"]["202"]
    assert "IntegrationKey" in schema["components"]["securitySchemes"]
    assert (
        "acknowledged_document_warnings"
        in schema["components"]["schemas"]["InvoiceApproval"]["properties"]
    )
    assert "/api/v1/specifications/{job_id}/result.json" in paths
