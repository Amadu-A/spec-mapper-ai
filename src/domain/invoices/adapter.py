# src/domain/invoices/adapter.py
"""Lossless адресация счетов и производные признаки для единственного matching engine."""

from __future__ import annotations

from collections.abc import Iterator


def invoice_items(source: dict) -> Iterator[tuple[dict, dict, dict, dict]]:
    """Обходит все строки, включая повторы/платежи, с устойчивой обратной адресацией."""
    for file in source["files"]:
        for document_index, document in enumerate(file["documents"]):
            for item in document["items"]:
                yield (
                    file,
                    document,
                    item,
                    {
                        "file_index": file["file_index"],
                        "document_index": document_index,
                        "row_number": item["row_number"],
                        "source_pages": item.get("source_pages", []),
                        "source_sha256": file["source_sha256"],
                    },
                )


def matching_source(item: dict) -> dict[str, str]:
    """Передаёт только подтверждённые brand/article; null меняется лишь в derived DTO."""
    return {
        "Caption": item["caption"],
        "Units": item.get("units") or "",
        "Quantity": item.get("quantity") or "",
        "Brand": item.get("brand") or item.get("manufacturer") or "",
        "Article": item.get("article") or "",
        "Pages": ", ".join(map(str, item.get("source_pages", []))),
        "UserComment1": "",
        "Note": "",
    }


def matchable(item: dict) -> bool:
    """Платежи и неизвестные виды не отправляются в товарный embedding/search."""
    return item.get("row_kind", "product") in {"product", "service"}
