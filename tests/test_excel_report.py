# tests/test_excel_report.py
"""Единственный Excel спецификации: шаблон Extracting, immutable approval, literal text и доступ."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from openpyxl import load_workbook
from pydantic import SecretStr

from src.application.reports import TEMPLATE_PATH, export_xlsx
from src.main import create_app

HEADERS = [
    "Номер по схеме",
    "Литера по схеме",
    "Номенклатура",
    "Ед. изм.",
    "Кол-во",
    "Цена (договорная)",
    "Валюта",
    "% скидки",
    "Цена закупки",
    "Дата цены",
    "Цена (розничная)",
    "Сумма",
    "Бренд",
    "Артикул",
    "Раздел",
    "Источник/Наименование файла",
    "Шифр документа",
    "Страница документа",
    "Примечание",
    "Наименование 1С",
    "Код номенклатуры 1С",
    "GUID номенклатуры 1С",
    "Единица измерения 1С",
    "Статус сопоставления",
    "Индекс позиции Mapper",
]
COLUMNS = {
    "Caption": "C",
    "Units": "D",
    "Quantity": "E",
    "Brand": "M",
    "Article": "N",
    "UserComment1": "Q",
    "Pages": "R",
    "Note": "S",
}


@pytest.fixture
def approved_document():
    """Даёт дубли, настоящую ссылку 1С, иной internal UUID и явный null."""
    item = {
        "Caption": " Клапан DN50\nPN16 ",
        "Units": " шт. ",
        "Quantity": " 2,50 ",
        "Brand": "000Бренд",
        "Article": "00000006836",
        "UserComment1": " Проект 01 ",
        "Pages": "004–005",
        "Note": " Примечание\nс переносом ",
        "index": 0,
        "onec": {
            "name": "Клапан",
            "code": "00000123",
            "onec_guid": str(uuid4()),
            "guid": str(uuid4()),
            "unit": "шт",
        },
        "matching": {"status": "manual"},
    }
    return {
        "file_name": " 0001-проект.pdf ",
        "items": [
            item,
            {**deepcopy(item), "index": 1},
            {**deepcopy(item), "index": 2, "onec": None, "matching": {"status": "not_found"}},
        ],
    }


@pytest.mark.parametrize("count", [0, 1, 46, 47, 10000])
def test_template_layout_styles_and_row_boundaries(approved_document, count):
    """Сохраняет A:S, валюту, оформление и ровно нужные строки до/после края шаблона."""
    original = TEMPLATE_PATH.read_bytes()
    assert sha256(original).hexdigest() == (
        "008dd7b7130668acc55e1bfdf96cf4be3b8bc05c1e0c27df0668189a6f14f2c4"
    )
    report = deepcopy(approved_document)
    report["items"] = [{**report["items"][0], "index": index} for index in range(count)]
    reference = load_workbook(BytesIO(original)).active
    book = load_workbook(BytesIO(export_xlsx(report)))
    sheet = book["Лист_1"]
    assert book.sheetnames == ["Лист_1"] and sheet.max_column == 25
    assert sheet.max_row == count + 1
    assert [cell.value for cell in sheet[1]] == HEADERS
    for column in range(1, 20):
        source_column = column if column < 16 else max(16, column - 1)
        assert sheet.cell(1, column)._style == reference.cell(1, source_column)._style
    for letter, dimension in reference.column_dimensions.items():
        if dimension.min < 16:
            assert dict(sheet.column_dimensions[letter]) == dict(dimension)
    assert sheet.column_dimensions["P"].width == 30
    for source, target in [("P", "Q"), ("Q", "R"), ("R", "S")]:
        assert sheet.column_dimensions[target].width == reference.column_dimensions[source].width
    assert sheet.row_dimensions[1].height == reference.row_dimensions[1].height
    assert all(index <= count + 1 for index in sheet.row_dimensions)
    for row in {2, max(2, count // 2 + 1), count + 1} if count else []:
        sample = min(row, reference.max_row)
        assert sheet.row_dimensions[row].height == reference.row_dimensions[sample].height
        assert sheet[f"G{row}"].value == "RUB"
        assert sheet[f"Y{row}"].value == row - 2
        for column in range(1, 20):
            source_column = column if column < 16 else max(16, column - 1)
            assert sheet.cell(row, column)._style == reference.cell(sample, source_column)._style
        for letter in ["A", "B", "F", "H", "I", "J", "K", "L", "O"]:
            assert sheet[f"{letter}{row}"].value is None
    assert TEMPLATE_PATH.read_bytes() == original
    book.close()


def test_source_duplicates_and_six_onec_columns_are_exact(approved_document):
    """Сохраняет дубли/пробелы/нули, настоящий GUID и null без внутреннего UUID в V."""
    snapshot = deepcopy(approved_document)
    book = load_workbook(BytesIO(export_xlsx(snapshot)), data_only=False)
    sheet = book.active
    for index, row in enumerate(snapshot["items"], 2):
        for field, column in COLUMNS.items():
            assert sheet[f"{column}{index}"].value == row[field]
            assert sheet[f"{column}{index}"].data_type == "s"
        assert sheet[f"P{index}"].value == snapshot["file_name"]
        onec = row["onec"] or {}
        assert [sheet.cell(index, col).value for col in range(20, 26)] == [
            onec.get("name"),
            onec.get("code"),
            onec.get("onec_guid"),
            onec.get("unit"),
            row["matching"]["status"],
            row["index"],
        ]
    assert sheet["U2"].data_type == "s" and sheet["V2"].data_type == "s"
    assert sheet["V2"].value != snapshot["items"][0]["onec"]["guid"]
    assert [sheet.cell(2, col).value for col in range(1, 25)] == [
        sheet.cell(3, col).value for col in range(1, 25)
    ]
    assert approved_document == snapshot
    # Legacy UUID никогда не становится настоящим GUID даже при наличии выбранной карточки.
    snapshot["items"][0]["onec"]["onec_guid"] = None
    legacy = load_workbook(BytesIO(export_xlsx(snapshot))).active
    assert legacy["V2"].value is None and legacy["U2"].value == "00000123"


@pytest.mark.parametrize("text", ["", "=1+1", "+SUM(A1:A2)", "-2+3", "@SUM(A1)", "\t=1+1", "#REF!"])
def test_user_text_cannot_become_formula_or_error(approved_document, text):
    """XML хранит пользовательский текст строкой, включая формулы и названия Excel-ошибок."""
    report = deepcopy(approved_document)
    report["file_name"] = text
    row = report["items"][0]
    for field in COLUMNS:
        row[field] = text
    for field in ["name", "code", "onec_guid", "unit"]:
        row["onec"][field] = text
    content = export_xlsx(report)
    sheet = load_workbook(BytesIO(content), data_only=False).active
    for column in [*COLUMNS.values(), "P", "T", "U", "V", "W"]:
        cell = sheet[f"{column}2"]
        assert (cell.value or "") == text
        if text:
            assert cell.data_type == "s"
    with ZipFile(BytesIO(content)) as archive:
        assert b"<f>" not in archive.read("xl/worksheets/sheet1.xml")


def test_old_approval_without_extended_fields(approved_document):
    """Старый утверждённый JSON остаётся прежним, недостающие новые поля в Excel пусты."""
    report = deepcopy(approved_document)
    report.pop("file_name")
    for row in report["items"]:
        for field in ["Brand", "Article", "Note"]:
            row.pop(field)
    before = deepcopy(report)
    sheet = load_workbook(BytesIO(export_xlsx(report))).active
    assert all(sheet[f"{column}2"].value is None for column in ["P", "M", "N", "S"])
    assert report == before


async def test_only_approved_snapshot_download_owner_ttl_and_no_recalculation(
    system, source, monkeypatch
):
    """Маршрут прежний; queued/review дают 409, snapshot неизменен, owner/TTL дают 404/410."""
    container, *_ = system
    container.settings.api_keys = {"telegram": SecretStr("t" * 32)}
    headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "template-excel"}
    app = create_app(container)
    paths = app.openapi()["paths"]
    assert [
        path for path in paths if path.startswith("/api/v1/mappings/") and path.endswith(".xlsx")
    ] == ["/api/v1/mappings/{job_id}/result.xlsx"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        job_id = created.json()["job_id"]
        prefix = f"/api/v1/mappings/{job_id}"
        for stage in ["queued", "review"]:
            response = await client.get(prefix + "/result.xlsx", headers=headers)
            assert (
                response.status_code == 409 and response.json()["error"]["code"] == "not_approved"
            )
            if stage == "queued":
                await container.processor.process(job_id)
        status = (await client.get(prefix, headers=headers)).json()
        await client.post(
            prefix + "/approve", json={"revision": status["revision"]}, headers=headers
        )
        snapshot = (await client.get(prefix + "/approved.json", headers=headers)).json()
        original = (await client.get(prefix + "/source.json", headers=headers)).json()
        result = (await client.get(prefix + "/result.json", headers=headers)).json()

        async def forbidden(*args, **kwargs):
            """Запрещает повторный подбор/загрузку каталога и чтение текущих строк задания."""
            raise AssertionError("Excel должен читать только сохранённый approval snapshot")

        with monkeypatch.context() as patch:
            for target, name in [
                (container.mappings, "result"),
                (container.embeddings, "embed"),
                (container.index, "search"),
                (container.catalog, "items"),
            ]:
                patch.setattr(target, name, forbidden)
            excel = await client.get(prefix + "/result.xlsx", headers=headers)
        assert excel.status_code == 200 and excel.content.startswith(b"PK")
        assert "spreadsheetml.sheet" in excel.headers["content-type"]
        assert 'filename="mapping.xlsx"' in excel.headers["content-disposition"]
        assert (await client.get(prefix + "/approved.json", headers=headers)).json() == snapshot
        assert (
            (await client.get(prefix + "/source.json", headers=headers)).json()
            == original
            == source
        )
        assert (await client.get(prefix + "/result.json", headers=headers)).json() == result
        assert (
            await client.get(prefix + "/result.xlsx", headers={"X-API-Key": "t" * 32})
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/mappings/{uuid4()}/result.xlsx", headers=headers)
        ).status_code == 404
        for suffix in ["/approved.xlsx", "/export-excel"]:
            assert (await client.get(prefix + suffix, headers=headers)).status_code == 404
        container.jobs.clock = lambda: status["expires_at"] + 1
        assert (await client.get(prefix + "/result.xlsx", headers=headers)).status_code == 410
