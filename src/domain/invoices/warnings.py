# src/domain/invoices/warnings.py
"""Финансовые предупреждения без пересчёта/исправления исходных денежных значений."""

from src.domain.models import MapperError


def selection_warnings(row: dict) -> list[str]:
    """Показывает актуальное отсутствие реального GUID после ручного перевыбора."""
    warnings = [code for code in row.get("warnings", []) if code != "onec_guid_missing"]
    if row["selection"] and not row["selection"].get("onec_guid"):
        warnings.append("onec_guid_missing")
    return warnings


def financial_warnings(source: dict) -> list[dict]:
    """Отмечает документы, требующие явного согласия; не объединяет их реквизиты/суммы."""
    documents = [
        (file["file_index"], index, document)
        for file in source["files"]
        for index, document in enumerate(file["documents"])
    ]
    currencies = {doc.get("currency") for _, _, doc in documents if doc.get("currency")}
    suppliers = {
        (doc.get("supplier", {}).get("inn"), doc.get("supplier", {}).get("name"))
        for _, _, doc in documents
        if any(doc.get("supplier", {}).values())
    }
    result = []
    for file_index, document_index, doc in documents:
        codes = []
        validation = doc.get("validation", {})
        status = validation.get("status", "not_checkable")
        if status != "passed":
            codes.append("financial_" + status)
        if any(check.get("status") == "failed" for check in validation.get("checks", [])):
            codes.append("financial_check_failed")
        if validation.get("warnings"):
            codes.append("source_financial_warnings")
        if not doc.get("currency"):
            codes.append("currency_missing")
        if len(currencies) > 1:
            codes.append("different_currencies")
        if not doc.get("supplier", {}).get("name"):
            codes.append("supplier_missing")
        if len(suppliers) > 1:
            codes.append("different_suppliers")
        if any(
            item.get("row_kind", "product") in {"product", "service"}
            and (item.get("line_total_without_vat") is None or item.get("vat_rate") is None)
            for item in doc["items"]
        ):
            codes.append("financial_fields_missing")
        if len({item["vat_rate"] for item in doc["items"] if item.get("vat_rate")}) > 1:
            codes.append("different_vat_rates")
        if codes:
            result.append(
                {
                    "document_id": f"{file_index}:{document_index}",
                    "file_index": file_index,
                    "document_index": document_index,
                    "validation_status": status,
                    "codes": codes,
                    "requires_ack": True,
                }
            )
    return result


def approval_context(source: dict, acknowledged: list[str]) -> dict:
    """Проверяет согласие на каждый финансовый риск, сохраняя его исходный статус."""
    warnings = financial_warnings(source)
    required = {warning["document_id"] for warning in warnings}
    if len(acknowledged) != len(set(acknowledged)) or set(acknowledged) - required:
        raise MapperError(
            "invalid_warning_acknowledgment", "Некорректные подтверждения документов", 422
        )
    if required - set(acknowledged):
        raise MapperError(
            "financial_acknowledgment_required",
            "Подтвердите финансовые предупреждения каждого документа",
            409,
        )
    return {"acknowledged_document_warnings": acknowledged, "financial_warnings": warnings}
