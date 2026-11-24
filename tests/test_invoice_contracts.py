# tests/test_invoice_contracts.py
"""Границы invoice-контракта, реальные файлы, миграция и экспорт; внешние модели подменены."""

import asyncio
import hashlib
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine, func, insert, inspect, select
from test_dashboard_migrations import upgrade
from test_invoices import (
    AUTH,
    image_bytes,
    imported,
    pdf_bytes,
)
from test_invoices import (
    invoice_source as invoice_source,
)
from test_invoices import (
    invoice_system as invoice_system,
)

from src.application.invoice_reports import invoice_export, invoice_xlsx
from src.application.invoices.ports import Upload
from src.domain.invoices.models import validate_invoice
from src.domain.models import MapperError, digest
from src.infrastructure.database import (
    ApprovalRow,
    InvoiceUploadRow,
    JobRow,
    SpecificationUploadRow,
)
from src.infrastructure.invoices.extracting import HttpInvoiceExtraction
from src.infrastructure.invoices.uploads import InvoiceUploadInspector
from src.main import create_app


def source_job(source: dict) -> dict:
    """Создаёт синтетический подтверждённый снимок для чистых тестов экспорта."""
    count = source["summary"]["item_count"]
    return {
        "id": str(uuid4()),
        "revision": 2,
        "state": "approved",
        "data": {
            "source": source,
            "source_hash": digest(source),
            "catalog_version": "test-catalog",
            "algorithm_version": "test-algorithm",
            "approval": {"approved_at": 1.0},
            "rows": [
                {
                    "selection": None,
                    "status": "not_found",
                    "confirmed": True,
                    "manually_reviewed": True,
                    "candidates": [],
                    "warnings": [],
                }
                for _ in range(count)
            ],
        },
    }


@pytest.mark.parametrize("size", [1, 5000])
def test_excel_finances_literal_text_duplicates_null(invoice_source, size):
    """Маленькая/большая книга сохраняет деньги, нули, повторные строки и текст вместо формул."""
    source = deepcopy(invoice_source)
    doc = source["files"][0]["documents"][0]
    original = deepcopy(doc["items"][0])
    original.update(
        caption='=HYPERLINK("https://example.invalid","text")',
        article="000000000000123",
        quantity="12345678901234567890.000000001",
        vat_amount=None,
    )
    doc["items"] = [deepcopy(original) for _ in range(size)]
    source["summary"]["item_count"] = size
    validate_invoice(source)
    snapshot = invoice_export(source_job(source))
    book = load_workbook(BytesIO(invoice_xlsx(snapshot)), read_only=True)
    assert book.sheetnames == ["Документы", "Позиции", "Предупреждения"]
    rows = book["Позиции"].iter_rows()
    headers = [cell.value for cell in next(rows)]
    assert headers[-7:] == [
        "Код 1С",
        "Наименование 1С",
        "Реальный GUID 1С",
        "Ед. 1С",
        "Статус сопоставления",
        "Индекс Mapper",
        "Подтверждено",
    ]
    count = 0
    for row in rows:
        count += 1
        values = dict(zip(headers, row, strict=True))
        assert values["Наименование"].value == original["caption"]
        assert values["Наименование"].data_type == "s"
        assert values["Артикул"].value == original["article"]
        assert values["Количество"].value == original["quantity"]
        assert values["Цена без НДС"].value == original["unit_price_without_vat"]
        assert values["Сумма НДС"].value is None
        assert values["Реальный GUID 1С"].value is None
        assert not any(cell.data_type == "f" for cell in row)
    assert count == size
    book.close()
    assert digest(snapshot["files"][0]["documents"][0]["validation"]) == digest(doc["validation"])


def test_additive_invoice_migration_preserves_specifications():
    """Исполняет 0001→0006, сохраняя старый PDF, job и immutable approval."""
    engine = create_engine("sqlite://")
    identifier = str(uuid4())
    snapshot = {"items": [{"Caption": "Исходный снимок"}]}
    with engine.begin() as connection:
        for revision in ["0001", "0002", "0003", "0004", "0005"]:
            upgrade(connection, revision)
        connection.execute(
            insert(JobRow).values(
                id=identifier,
                owner="legacy",
                idempotency_hash="a" * 64,
                request_hash="b" * 64,
                kind="mapping",
                state="approved",
                stage="approved",
                created_at=1,
                expires_at=9999999999,
                attempts=1,
                revision=2,
                data={"source": snapshot},
            )
        )
        connection.execute(
            insert(SpecificationUploadRow).values(job_id=identifier, content=b"%PDF-original")
        )
        connection.execute(
            insert(ApprovalRow).values(
                job_id=identifier,
                owner="legacy",
                revision=2,
                approved_at=1,
                idempotency_hash="c" * 64,
                snapshot=snapshot,
            )
        )
        upgrade(connection, "0006")
        assert "invoice_uploads" in inspect(connection).get_table_names()
        assert connection.scalar(select(ApprovalRow.snapshot)) == snapshot
        assert connection.scalar(select(SpecificationUploadRow.content)) == b"%PDF-original"
        assert connection.scalar(select(JobRow.data)) == {"source": snapshot}
    engine.dispose()


@pytest.mark.parametrize(
    "status,code",
    [
        (404, "upstream_not_found"),
        (410, "upstream_expired"),
        (502, "upstream_http_error"),
        (504, "upstream_http_error"),
    ],
)
async def test_invoice_upstream_http_errors(settings, status, code):
    """Ошибки upstream не становятся пустым успешным source и не раскрывают тела/ключи."""

    async def handler(request):
        """Отдаёт только управляемый HTTP отказ, без настоящей VLM."""
        return httpx.Response(status, text="private-upstream-document")

    async def progress(stage, **details):
        """Принимает этап безопасной диагностики без содержимого документа."""
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MapperError) as error:
            await HttpInvoiceExtraction(client, settings).fetch(str(uuid4()), progress)
        assert error.value.code == code
        assert "private-upstream-document" not in str(error.value)
        assert "f" * 32 not in str(error.value)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
async def test_invoice_upstream_terminal_errors(settings, state):
    """Терминальный отказ 8083 различается по коду и не запускает matching."""

    async def handler(request):
        """Возвращает реальную форму status с заданным terminal состоянием."""
        return httpx.Response(200, json={"status": state})

    async def progress(stage, **details):
        """Принимает только технический этап."""
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MapperError) as error:
            await HttpInvoiceExtraction(client, settings).fetch(str(uuid4()), progress)
        assert error.value.code == "upstream_" + state


async def test_upload_lost_submit_response_uses_same_key(invoice_system, invoice_source):
    """Принятый 8083 POST с потерянным ответом повторяется по тому же внешнему job key."""
    c, _ = invoice_system
    content = pdf_bytes()

    async def chunks():
        """Передаёт один синтетический PDF ограниченным upload-адаптером."""
        yield content

    job = await c.invoices.submit(
        "owner", "lost-response", [Upload("one.pdf", "application/pdf", chunks())], {}
    )
    source = deepcopy(invoice_source)
    source["files"][0]["source_sha256"] = hashlib.sha256(content).hexdigest()
    calls = []

    async def handler(request):
        """Моделирует устойчивый idempotent upstream после потери первого ответа."""
        if request.method == "POST":
            calls.append(request.headers["Idempotency-Key"])
            if len(calls) == 1:
                raise httpx.ReadError("lost response", request=request)
            return httpx.Response(202, json={"job_id": str(uuid4())})
        if request.url.path.endswith("result.json"):
            return httpx.Response(200, json=source)
        return httpx.Response(200, json={"status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        c.processor.invoice_extraction = HttpInvoiceExtraction(client, c.settings)
        await c.processor.process(job["id"])
        assert (await c.jobs.get(job["id"]))["state"] == "queued"
        await c.processor.process(job["id"])
        assert (await c.jobs.get(job["id"]))["state"] == "ready_for_review"
        assert calls == [job["id"], job["id"]]


async def test_invoice_units_and_guid_missing_are_review(invoice_system, invoice_source):
    """Упаковка не пересчитывается в штуки; ручной выбор не синтезирует GUID 1С."""
    c, catalog = invoice_system
    source = deepcopy(invoice_source)
    source["files"][0]["documents"][0]["items"][0]["units"] = "упак"
    job = await c.invoices.import_document("owner", "uom", source)
    await c.processor.process(job["id"])
    job = await c.jobs.get(job["id"])
    row = job["data"]["rows"][0]
    assert row["status"] == "review"
    assert "Несовместимые единицы измерения" in row["candidates"][0]["conflicts"]
    row["selection"] = {**catalog[0].to_dict(), "onec_guid": None}
    snapshot = invoice_export(job)
    exported = snapshot["files"][0]["documents"][0]
    assert "onec_guid_missing" in exported["items"][0]["matching"]["warnings"]
    assert exported["items"][0]["onec"]["onec_guid"] is None
    assert exported["ready_for_onec"] is False


@pytest.mark.parametrize(
    "media,content",
    [("image/png", b"\x89PNG\r\n\x1a\nbroken"), ("application/pdf", b"%PDF-1.7\nbroken")],
)
def test_corrupt_files_do_not_pass_magic_only(media, content):
    """Одного правильного заголовка недостаточно для принятия повреждённого файла."""
    with pytest.raises(MapperError) as error:
        InvoiceUploadInspector().inspect(content, media)
    assert error.value.code == "invalid_invoice_file"


def test_jpeg_and_swagger_integrity():
    """Настоящий JPEG принимается; vendored Swagger совпадает с pinned SHA-256 manifest."""
    assert InvoiceUploadInspector().inspect(image_bytes("JPEG"), "image/jpeg") == ("image/jpeg", 1)
    root = Path("src/resources/swagger_ui")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "v5.36.0"
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected


async def test_invoice_chunked_body_and_page_budget(invoice_system):
    """Поток без Content-Length ограничен до multipart parsing; pages суммируются по всем файлам."""
    c, _ = invoice_system
    c.settings.invoice_max_batch_bytes = 1024

    async def body():
        """Управляемый oversize chunk без неограниченной аллокации в приложении."""
        yield b"x" * (655360 + 1025)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/invoices",
            headers={**AUTH, "Content-Type": "multipart/form-data; boundary=test"},
            content=body(),
        )
        assert response.status_code == 413, response.text
    c.settings.invoice_max_batch_bytes = 100 * 1024 * 1024
    c.settings.invoice_max_selected_pages = 1

    async def chunks():
        """Две настоящие физические страницы превосходят настроенный лимит в одну."""
        yield pdf_bytes(2)

    with pytest.raises(MapperError) as error:
        await c.invoices.submit(
            "owner", "page-budget", [Upload("one.pdf", "application/pdf", chunks())], {}
        )
    assert error.value.code == "page_limit"


async def test_real_local_corpus_lossless(invoice_system):
    """Проверяет семь локальных PDF/JSON и 161 строку; AI/search doubles не являются live."""
    folder = Path("../spec-extracting-ai/examples/invoices")
    if not await asyncio.to_thread(folder.is_dir):
        pytest.skip("Локальный приватный corpus Extracting не установлен")
    paths = await asyncio.to_thread(lambda: sorted(folder.glob("*.json")))
    assert len(paths) == 7
    c, _ = invoice_system
    count = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test"
    ) as client:
        for index, path in enumerate(paths):
            source = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8-sig"))
            validate_invoice(source)
            content = await asyncio.to_thread(path.with_suffix(".pdf").read_bytes)
            assert (
                await asyncio.to_thread(
                    InvoiceUploadInspector().inspect, content, "application/pdf"
                )
            )[1] == source["files"][0]["page_count"]
            identifier = await imported(client, source, key=f"corpus-{index}")
            await c.processor.process(identifier)
            root = "/api/v1/invoices/" + identifier
            page = (await client.get(root + "/rows", headers=AUTH)).json()
            assert page["processed"] == source["summary"]["item_count"]
            assert digest((await client.get(root + "/source.json", headers=AUTH)).json()) == digest(
                source
            )
            acknowledged = [warning["document_id"] for warning in page["financial_warnings"]]
            response = await client.post(
                root + "/approve",
                headers=AUTH,
                json={"revision": page["revision"], "acknowledged_document_warnings": acknowledged},
            )
            assert response.status_code == 200
            snapshot = (await client.get(root + "/result.json", headers=AUTH)).json()
            for file, original_file in zip(snapshot["files"], source["files"], strict=True):
                for doc, original_doc in zip(
                    file["documents"], original_file["documents"], strict=True
                ):
                    assert digest(doc["validation"]) == digest(original_doc.get("validation"))
                    for item, original_item in zip(
                        doc["items"], original_doc["items"], strict=True
                    ):
                        assert digest(
                            {
                                key: value
                                for key, value in item.items()
                                if key not in {"onec", "matching"}
                            }
                        ) == digest(original_item)
            assert (await client.get(root + "/result.xlsx", headers=AUTH)).status_code == 200
            count += page["total"]
    assert count == 161


async def test_exact_default_byte_and_page_boundaries(invoice_system):
    """Реальные 50/100 MiB и 100 страниц принимаются; превышение не создаёт частичных blobs."""
    c, _ = invoice_system
    small = pdf_bytes()
    target = 50 * 1024 * 1024
    marker = small.rfind(b"startxref")
    extra = target - len(small)
    content = small[:marker] + b"%" + b"x" * (extra - 2) + b"\n" + small[marker:]
    assert len(content) == target

    def upload(body: bytes) -> Upload:
        """Даёт фактические MiB-блоки, как HTTP-адаптер, сохраняя границы тела."""

        async def chunks():
            """Не передаёт целый большой файл одним неограниченным chunk."""
            for offset in range(0, len(body), 1024 * 1024):
                yield body[offset : offset + 1024 * 1024]

        return Upload("boundary.pdf", "application/pdf", chunks())

    accepted = await c.invoices.submit(
        "boundary", "exact-cap", [upload(content), upload(content)], {}
    )
    stored = await c.jobs.invoice_files(accepted["id"])
    assert len(stored) == 2 and sum(map(len, stored)) == 100 * 1024 * 1024
    del stored
    for key, files in [
        ("file-over-cap", [upload(content + b"x")]),
        ("batch-over-cap", [upload(content), upload(content), upload(small)]),
    ]:
        with pytest.raises(MapperError) as error:
            await c.invoices.submit("boundary", key, files, {})
        assert error.value.code == "upload_limit"
    async with c.jobs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(InvoiceUploadRow)) == 2
    pages = pdf_bytes(101)
    accepted_pages = await c.invoices.submit(
        "boundary", "100-pages", [upload(pages)], {"0": "1-100"}
    )
    assert len(accepted_pages["data"]["invoice"]["files"][0]["selected_pages"]) == 100
    with pytest.raises(MapperError) as error:
        await c.invoices.submit("boundary", "101-pages", [upload(pages)], {})
    assert error.value.code == "page_limit"
