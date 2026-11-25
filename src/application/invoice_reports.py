# src/application/invoice_reports.py
"""Invoice JSON/Excel из одного snapshot; исходные финансы и строки не пересчитываются."""

import json
from collections import Counter
from copy import deepcopy
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill

from src.domain.invoices.adapter import invoice_items
from src.domain.invoices.warnings import selection_warnings
from src.observability import timed

ITEM_COLUMNS = (
    ("row_number", "Строка"),
    ("caption", "Наименование"),
    ("brand", "Бренд"),
    ("manufacturer", "Производитель"),
    ("article", "Артикул"),
    ("article_candidate", "Артикул (кандидат)"),
    ("supplier_code", "Код поставщика"),
    ("product_reference_raw", "Исходная ссылка"),
    ("row_kind", "Вид строки"),
    ("units", "Единица"),
    ("quantity", "Количество"),
    ("unit_price_without_vat", "Цена без НДС"),
    ("unit_price_with_vat", "Цена с НДС"),
    ("line_total_without_vat", "Сумма без НДС"),
    ("vat_rate", "НДС, %"),
    ("vat_amount", "Сумма НДС"),
    ("line_total_with_vat", "Сумма с НДС"),
    ("vat_rate_source", "Источник ставки НДС"),
)


def invoice_export(job: dict) -> dict:
    """Добавляет onec/matching рядом с оригинальными полями, не создавая второго onec."""
    data = job["data"]
    result = deepcopy(data["source"])
    for (_, _, item, _), row, index in zip(
        invoice_items(result), data["rows"], range(len(data["rows"])), strict=True
    ):
        selection = row["selection"]
        warnings = selection_warnings(row)
        item["onec"] = deepcopy(selection)
        item["matching"] = {
            "row_index": index,
            "status": row["status"],
            "confirmed": row["confirmed"],
            "manually_reviewed": row.get("manually_reviewed", False),
            "approved": job["state"] == "approved",
            "catalog_version": (selection or {}).get("catalog_version", data["catalog_version"]),
            "score": next(
                (
                    candidate["score"]
                    for candidate in row["candidates"]
                    if selection and candidate["item"]["guid"] == selection["guid"]
                ),
                None,
            ),
            "warnings": warnings,
        }
    for file in result["files"]:
        for index, document in enumerate(file["documents"]):
            document["document_index"] = index
            document["matching_summary"] = dict(
                Counter(item["matching"]["status"] for item in document["items"])
            )
            document["ready_for_onec"] = bool(document["items"]) and all(
                item["matching"]["confirmed"] and item["onec"] and item["onec"].get("onec_guid")
                for item in document["items"]
            )
    result.update(
        schema_version="invoice_mapping.v1",
        job_id=job["id"],
        status=job["state"],
        revision=job["revision"],
        source_hash=data["source_hash"],
        catalog_version=data["catalog_version"],
        algorithm_version=data["algorithm_version"],
        approved_at=data.get("approval", {}).get("approved_at"),
        approval=deepcopy(data.get("approval_context", data.get("approval", {}))),
    )
    if result["status"] in {"complete", "ready_for_review"}:
        result["status"] = "awaiting_review"
    return result


def append_text(sheet, values: list) -> None:
    """Пишет lossless literal text, включая деньги: без float, Excel-формул и потери нулей."""
    cells = []
    for value in values:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        cells.append(cell)
    sheet.append(cells)


def report_sheet(book: Workbook, name: str, headers: list[str]):
    """Создаёт финансовый лист с закреплённым заголовком и общим оформлением."""
    sheet = book.create_sheet(name)
    sheet.freeze_panes = "C2"
    cells = []
    for value in headers:
        cell = WriteOnlyCell(sheet, value=value)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1D3557")
        cells.append(cell)
    sheet.append(cells)
    return sheet


@timed("invoice_export_xlsx")
def invoice_xlsx(snapshot: dict) -> bytes:
    """Строит отдельную книгу только из утверждённого JSON, без модели/каталога/HTTP."""
    book = Workbook(write_only=True)
    documents = report_sheet(
        book,
        "Документы",
        [
            "Файл",
            "Документ",
            "Вид",
            "Номер",
            "Дата",
            "Поставщик",
            "ИНН поставщика",
            "КПП поставщика",
            "Покупатель",
            "ИНН покупателя",
            "КПП покупателя",
            "Валюта",
            "Позиций",
            "Без НДС",
            "НДС",
            "С НДС",
            "Проверка",
            "Страницы",
            "Приложения",
            "Условия оплаты",
            "Корректировки",
            "Примечания",
            "Проверки",
            "Готов к заполнению 1С",
        ],
    )
    items = report_sheet(
        book,
        "Позиции",
        [
            "Файл",
            "Документ",
            *[label for _, label in ITEM_COLUMNS],
            "Страницы",
            "Происхождение полей",
            "Производные поля",
            "Технические страницы",
            "Исходная позиция JSON",
            "Код 1С",
            "Наименование 1С",
            "Реальный GUID 1С",
            "Ед. 1С",
            "Статус сопоставления",
            "Индекс Mapper",
            "Подтверждено",
        ],
    )
    warnings = report_sheet(book, "Предупреждения", ["Файл", "Документ", "Строка", "Код", "Данные"])
    for file in snapshot["files"]:
        for document in file["documents"]:
            supplier, buyer = document.get("supplier", {}), document.get("buyer", {})
            totals, validation = document.get("totals", {}), document.get("validation", {})
            base = [file["source_file"], document["document_index"]]
            append_text(
                documents,
                [
                    *base,
                    document.get("document_type"),
                    document.get("number"),
                    document.get("date"),
                    supplier.get("name"),
                    supplier.get("inn"),
                    supplier.get("kpp"),
                    buyer.get("name"),
                    buyer.get("inn"),
                    buyer.get("kpp"),
                    document.get("currency"),
                    len(document["items"]),
                    totals.get("without_vat"),
                    totals.get("vat"),
                    totals.get("with_vat"),
                    validation.get("status"),
                    document.get("source_pages"),
                    document.get("attached_pages"),
                    document.get("payment_terms"),
                    totals.get("adjustments"),
                    document.get("notes"),
                    validation.get("checks"),
                    document["ready_for_onec"],
                ],
            )
            for item in document["items"]:
                onec, matching = item["onec"] or {}, item["matching"]
                append_text(
                    items,
                    [
                        *base,
                        *[item.get(key) for key, _ in ITEM_COLUMNS],
                        item.get("source_pages"),
                        item.get("field_origin"),
                        item.get("derived_fields"),
                        item.get("technical_parameters_pages"),
                        {k: v for k, v in item.items() if k not in {"onec", "matching"}},
                        onec.get("code"),
                        onec.get("name"),
                        onec.get("onec_guid"),
                        onec.get("unit"),
                        matching["status"],
                        matching["row_index"],
                        matching["confirmed"],
                    ],
                )
                for warning in matching["warnings"]:
                    append_text(warnings, [*base, item["row_number"], warning, None])
            for warning in validation.get("warnings", []):
                append_text(warnings, [*base, None, "source_warning", warning])
            for check in validation.get("checks", []):
                if check.get("status") != "passed":
                    append_text(
                        warnings, [*base, check.get("row_number"), check.get("check"), check]
                    )
    for warning in snapshot.get("approval", {}).get("financial_warnings", []):
        append_text(
            warnings,
            [
                warning["file_index"],
                warning["document_index"],
                None,
                "acknowledged_financial_warning",
                warning,
            ],
        )
    output = BytesIO()
    book.save(output)
    return output.getvalue()
