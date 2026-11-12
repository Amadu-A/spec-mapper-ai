# tests/test_specification_v4.py
"""Сквозной контракт Extracting V4: дословный source, JSON/Excel, legacy и качество подбора."""

from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from src.domain.matching import choose, rerank, specification_text
from src.domain.models import SOURCE_FIELDS, CatalogItem, MapperError, validate_source
from src.infrastructure.database import ApprovalRow
from src.infrastructure.http_clients import HttpExtractionSource
from src.main import create_app


@pytest.fixture
def extended_source():
    """Пример манометра ROSMA и дословные дубли со строковыми дробями/формулой/HTML."""
    row = {
        "Caption": "Манометр радиальный Д100 ТМ-510Р.00",
        "Units": "шт.",
        "Quantity": "0,5",
        "UserComment1": "267-РД-ИТП.УУТЭ",
        "Pages": "7",
        "Brand": "ROSMA",
        "Article": "00000006836",
        "Note": ' Уточнить при монтаже\n<img src=x onerror="alert(1)"> =SUM(A1:A2) ',
    }
    return {
        "file_name": "spec_267-РД-ИТП.УУТЭ.pdf",
        "items": [deepcopy(row), deepcopy(row), {**row, "Brand": "", "Article": "", "Note": ""}],
    }


async def test_extended_spec_roundtrip_api_pg_approval_xlsx(system, extended_source):
    """Все исходные реквизиты проходят PG, таблицу/поиск, null approve, JSON и literal Excel."""
    container, *_ = system
    document = deepcopy(extended_source)
    document["items"][0]["Brand"] = '=HYPERLINK("http://invalid.test")'
    headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "v4-spec"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/mappings/import-json", json=document, headers=headers)
        assert response.status_code == 202, response.text
        job = response.json()["job_id"]
        await container.processor.process(job)
        prefix = f"/api/v1/mappings/{job}"
        assert (await client.get(prefix + "/source.json", headers=headers)).json() == document
        result = (await client.get(prefix + "/result", headers=headers)).json()
        assert result["file_name"] == document["file_name"]
        assert [row["source"] for row in result["items"]] == document["items"]
        filtered = (
            await client.get(prefix + "/result?q=00000006836&limit=1&offset=1", headers=headers)
        ).json()
        assert filtered["items"][0]["index"] == 1 and filtered["filtered_total"] == 2
        for row in result["items"]:
            cleared = await client.delete(
                prefix + f"/rows/{row['index']}/selection",
                params={"revision": result["revision"]},
                headers=headers,
            )
            assert cleared.status_code == 200
            result["revision"] = cleared.json()["revision"]
        export = (await client.get(prefix + "/result.json", headers=headers)).json()
        assert export["file_name"] == document["file_name"]
        for actual, expected in zip(export["items"], document["items"], strict=True):
            assert {key: actual[key] for key in SOURCE_FIELDS} == expected and actual[
                "onec"
            ] is None
        approved = await client.post(
            prefix + "/approve", json={"revision": result["revision"]}, headers=headers
        )
        assert approved.status_code == 200
        snapshot = (await client.get(prefix + "/approved.json", headers=headers)).json()
        assert snapshot["file_name"] == document["file_name"]
        assert all(
            row["onec"] is None and row["matching"]["confirmed"] for row in snapshot["items"]
        )
        excel = await client.get(prefix + "/result.xlsx", headers=headers)
        sheet = load_workbook(BytesIO(excel.content), data_only=False).active
        columns = {cell.value: cell.column for cell in sheet[1]}
        for index, row in enumerate(document["items"], 2):
            for name, expected in row.items():
                cell = sheet.cell(index, columns[name])
                assert (cell.value or "") == expected
                if expected:
                    assert cell.data_type == "s"
            assert sheet.cell(index, columns["file_name"]).value == document["file_name"]
        assert (await client.get(prefix + "/source.json", headers=headers)).json() == document
        assert (
            await client.get(prefix + "/approved.json", headers={"X-API-Key": "bad"})
        ).status_code == 401


async def test_legacy_source_and_old_approved_snapshot_not_rewritten(system, source):
    """Дополняет только derived legacy; исторический approved остаётся неизменным."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "legacy-v4", source)
    await container.processor.process(job["id"])
    result = await container.mappings.result("owner", job["id"])
    await container.mappings.approve("owner", job["id"], result["revision"], "approve-legacy")
    snapshot = await container.jobs.approved(job["id"], "owner")
    snapshot.pop("file_name")
    for row in snapshot["items"]:
        for name in ("Brand", "Article", "Note"):
            row.pop(name)
    async with container.catalog.sessions.begin() as session:
        approval = await session.scalar(select(ApprovalRow).where(ApprovalRow.job_id == job["id"]))
        approval.snapshot = deepcopy(snapshot)
    assert await container.jobs.approved(job["id"], "owner") == snapshot
    assert (await container.jobs.get(job["id"]))["data"]["source"] == source


async def test_upstream_extended_polling_contract(settings, extended_source):
    """Настоящий HttpExtractionSource получает восемь полей и file_name через mock upstream."""
    calls = []

    def handle(request):
        """Только окончательный completed/result.json, API-ключ не теряется."""
        calls.append(request)
        assert request.headers["X-API-Key"] == "f" * 32
        return httpx.Response(
            200,
            json=extended_source
            if request.url.path.endswith("result.json")
            else {"status": "completed_with_warnings"},
        )

    async def progress(stage):
        """Принимает фактический fetching без публикации partial."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        payload = await HttpExtractionSource(client, settings).fetch(str(uuid4()), progress)
    assert payload == extended_source and len(calls) == 2


@pytest.mark.parametrize(
    "mutation", ["filename", "missing", "partial", "extra", "numeric", "brandlimit"]
)
def test_versioned_source_is_strict(extended_source, mutation):
    """Версия не угадывается по частично заполненным полям; лишние/числовые значения отклоняются."""
    if mutation == "filename":
        extended_source["file_name"] = 7
    elif mutation == "missing":
        extended_source["items"][0].pop("Article")
    elif mutation == "partial":
        extended_source.pop("file_name")
    elif mutation == "extra":
        extended_source["items"][0]["unknown"] = "x"
    elif mutation == "numeric":
        extended_source["items"][0]["Quantity"] = 0.5
    else:
        extended_source["items"][0]["Brand"] = "x" * 2001
    with pytest.raises(MapperError):
        validate_source(extended_source)


def test_explicit_brand_article_conflict_and_generic_note(extended_source):
    """Высокий cosine не скрывает Brand/Article mismatch; Note/file_name не влияют на embedding."""
    row = extended_source["items"][0]
    exact = CatalogItem("id", "0001", row["Caption"], "шт", article=row["Article"], brand="ROSMA")
    assert choose(rerank(row, [(exact, 1.0)]), 0.88, 0.05)["status"] == "matched"
    wrong = CatalogItem("wrong", "0002", row["Caption"], "шт", article="6836", brand="Other")
    assert choose(rerank(row, [(wrong, 1.0)]), 0.1, 0)["selection"] is None
    assert specification_text(row) == specification_text(
        {**row, "Note": "Уточнить при монтаже", "file_name": "другое.pdf"}
    )
    assert "00000006836" in specification_text(row)


async def test_openapi_versions_require_complete_fields_and_preserve_source_hash(
    system, extended_source
):
    """OpenAPI описывает два точных входа, file_name входит в identity исходного документа."""
    from src.domain.models import digest

    container, *_ = system
    schema = create_app(container).openapi()
    models = schema["components"]["schemas"]
    assert set(models["ExtendedSourceDocument"]["required"]) == {"file_name", "items"}
    assert set(models["ExtendedSourceRow"]["required"]) == set(SOURCE_FIELDS)
    assert set(models["LegacySourceDocument"]["properties"]) == {"items"}
    assert set(models["LegacySourceRow"]["properties"]) == set(SOURCE_FIELDS[:5])
    assert digest(extended_source) != digest({**extended_source, "file_name": "другое.pdf"})
