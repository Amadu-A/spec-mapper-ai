# src/application/reports.py
"""Экспорт с отдельными полями 1С и сохранением всех исходных строк, включая Excel-формулы."""

from __future__ import annotations

import json
from copy import copy
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.domain.models import source_view
from src.observability import timed

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "resources/specification_v1.xlsx"
ITEM_COLUMNS = {
    "Caption": "C",
    "Units": "D",
    "Quantity": "E",
    "UserComment1": "Q",
    "Pages": "R",
    "Brand": "M",
    "Article": "N",
    "Note": "S",
}
MAPPING_HEADERS = (
    "Наименование 1С",
    "Код номенклатуры 1С",
    "GUID номенклатуры 1С",
    "Единица измерения 1С",
    "Статус сопоставления",
    "Индекс позиции Mapper",
)


def export_json(job: dict) -> dict:
    """Возвращает восемь исходных реквизитов и независимый выбор без изменения source."""
    data = job["data"]
    return {
        "job_id": job["id"],
        "file_name": data["source"].get("file_name", ""),
        "source_job_id": data.get("source_job_id"),
        "source_hash": data["source_hash"],
        "catalog_version": data["catalog_version"],
        "algorithm_version": data["algorithm_version"],
        "status": job["state"],
        "approved_at": data.get("approval", {}).get("approved_at"),
        "approved_revision": data.get("approval", {}).get("approved_revision"),
        "items": [
            {
                **source_view(source),
                "index": index,
                "onec": row["selection"],
                "matching": {
                    "status": row["status"],
                    "confirmed": row["confirmed"],
                    "manually_reviewed": row.get("manually_reviewed", False),
                    "approved": job["state"] == "approved",
                    "score": next(
                        (
                            candidate["score"]
                            for candidate in row["candidates"]
                            if row["selection"]
                            and candidate["item"]["guid"] == row["selection"]["guid"]
                        ),
                        None,
                    ),
                },
            }
            for index, (source, row) in enumerate(
                zip(data["source"]["items"], data["rows"], strict=True)
            )
        ],
    }


@timed("mapping_export_xlsx")
def export_xlsx(snapshot: dict) -> bytes:
    """Заполняет локальный шаблон Extracting только из неизменяемого утверждённого снимка."""
    workbook = load_workbook(TEMPLATE_PATH)
    sheet = workbook["Лист_1"]
    add_source_column(sheet)
    sample_row = sheet.max_row
    sample_cells = [(cell.column, copy(cell._style), cell.value) for cell in sheet[sample_row]]
    sample_dimension = copy(sheet.row_dimensions[sample_row])
    for column, (header, width) in enumerate(
        zip(MAPPING_HEADERS, (63, 28, 38, 24, 26, 26), strict=True), start=20
    ):
        cell = sheet.cell(1, column, header)
        cell._style = copy(sheet["S1"]._style)
        sheet.column_dimensions[get_column_letter(column)].width = width
    for index, row in enumerate(snapshot["items"], start=2):
        if index > sample_row:
            dimension = copy(sample_dimension)
            dimension.index = index
            sheet.row_dimensions[index] = dimension
            for column, style, value in sample_cells:
                target = sheet.cell(index, column)
                target._style = copy(style)
                target.value = value
        for field, column in ITEM_COLUMNS.items():
            cell = sheet[f"{column}{index}"]
            cell.value = row.get(field, "")
            cell.data_type = "s"
        source = sheet[f"P{index}"]
        source.value = snapshot.get("file_name", "")
        source.data_type = "s"
        item = row["onec"] or {}
        values = [
            item.get("name", ""),
            item.get("code", ""),
            item.get("onec_guid", ""),
            item.get("unit", ""),
            row["matching"]["status"],
            row["index"],
        ]
        for column, value in enumerate(values, start=20):
            cell = sheet.cell(index, column, value)
            cell._style = copy(sheet[f"S{index}"]._style)
            if isinstance(value, str):
                cell.data_type = "s"
    last_row = len(snapshot["items"]) + 1
    if last_row < sample_row:
        sheet.delete_rows(last_row + 1, sample_row - last_row)
        for index in list(sheet.row_dimensions):
            if index > last_row:
                del sheet.row_dimensions[index]
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def add_source_column(sheet: Worksheet) -> None:
    """Повторяет добавление графы P из Extracting, сохраняя стили и ширину исходных A:S."""
    sheet.insert_cols(16)
    for column in range(18, 15, -1):
        target = get_column_letter(column + 1)
        dimension = copy(sheet.column_dimensions[get_column_letter(column)])
        dimension.index = target
        dimension.min = dimension.max = column + 1
        sheet.column_dimensions[target] = dimension
    sheet.column_dimensions["P"].width = 30
    for index in range(1, sheet.max_row + 1):
        sheet[f"P{index}"]._style = copy(sheet[f"Q{index}"]._style)
    sheet["P1"] = "Источник/Наименование файла"


def export_catalog_xlsx(report: dict) -> bytes:
    """Выгружает только технические реквизиты run и этапы, без строк номенклатуры/документов."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Импорт каталога")
    sheet.append(["Параметр", "Значение"])
    for key, value in report.items():
        if key == "events":
            continue
        cell = WriteOnlyCell(
            sheet,
            value=json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
            if value is not None
            else "",
        )
        cell.data_type = "s"
        sheet.append([key, cell])
    events = workbook.create_sheet("Этапы UTC")
    events.append(["Unix UTC", "Этап", "Технические сведения"])
    for event in report.get("events", []):
        values = [
            event["at"],
            event["stage"],
            json.dumps(
                {key: value for key, value in event.items() if key not in {"at", "stage"}},
                ensure_ascii=False,
            ),
        ]
        cells = []
        for value in values:
            cell = WriteOnlyCell(events, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        events.append(cells)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
