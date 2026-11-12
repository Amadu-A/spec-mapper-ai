# src/application/reports.py
"""Экспорт с отдельными полями 1С и сохранением всех исходных строк, включая Excel-формулы."""

from __future__ import annotations

import json
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

from src.domain.models import SOURCE_FIELDS, source_view
from src.observability import timed


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
def export_xlsx(job: dict) -> bytes:
    """Пишет все исходные строки как literal text: формулы не исполняются и текст не изменяется."""
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Сопоставление")
    headers = [
        *SOURCE_FIELDS,
        "Наименование 1С",
        "Код 1С",
        "GUID 1С",
        "Внутренний UUID",
        "Единица 1С",
        "Статус",
        "Cosine-score",
        "Проверено человеком",
        "Версия каталога",
        "file_name",
        "Марка 1С",
        "Артикул 1С",
        "Производитель 1С",
    ]
    sheet.freeze_panes = "A2"
    sheet.column_dimensions["A"].width = 65
    sheet.column_dimensions["F"].width = 65
    for column in "BCDEGHIJKLMN":
        sheet.column_dimensions[column].width = 22
    cells = []
    for header in headers:
        cell = WriteOnlyCell(sheet, value=header)
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="244B76")
        cells.append(cell)
    sheet.append(cells)
    report = export_json(job)
    for row in report["items"]:
        item, match = row["onec"] or {}, row["matching"]
        values = [
            *[row[field] for field in SOURCE_FIELDS],
            item.get("name", ""),
            item.get("code", ""),
            item.get("onec_guid", ""),
            item.get("guid", ""),
            item.get("unit", ""),
            match["status"],
            match["score"],
            "Да" if match["confirmed"] else "Нет",
            job["data"]["catalog_version"],
            report["file_name"],
            item.get("brand", ""),
            item.get("article", ""),
            item.get("manufacturer", ""),
        ]
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


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
