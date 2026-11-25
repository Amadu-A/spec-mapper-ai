# src/domain/lifecycle.py
"""Безопасные представления жизненного цикла и техническая идентичность входа Mapper."""

import json
from collections import Counter
from copy import deepcopy

from src.domain.invoices.warnings import financial_warnings
from src.domain.models import digest

USER_KINDS = {"mapping", "invoice"}
ACTIVE = {"queued", "polling", "fetching", "extracting", "matching", "retry", "cancelling"}
REVIEW = {"complete", "ready_for_review"}


def profile(job: dict) -> str:
    """Разделяет бизнес-профили без переименования исторического kind=mapping."""
    return "invoice" if job["kind"] == "invoice" else "specification"


def api_root(job: dict) -> str:
    """Сохраняет исторические адреса и выбирает правильный профиль экспорта."""
    name = (
        "invoices"
        if job["kind"] == "invoice"
        else ("specifications" if job["data"].get("specification") else "mappings")
    )
    return "/api/v1/" + name


def status(job: dict) -> str:
    """Нормализует состояние UI, сохраняя внутренний state и прежние REST DTO."""
    state = job["state"]
    if state in REVIEW:
        return "awaiting_review"
    if state in {"queued", "approved", "failed", "cancelling", "cancelled"}:
        return state
    return "matching" if job["data"].get("source") else "extracting"


def byte_size(value: object) -> int:
    """Считает логические UTF-8 bytes сериализации SQLAlchemy, а не размер сжатых страниц БД."""
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def canonical_pages(value: str) -> list[int] | str:
    """Сводит технически одинаковые диапазоны без исправления исходного запроса."""
    pages = set()
    try:
        for part in value.split(","):
            bounds = [int(item.strip()) for item in part.strip().split("-")]
            first, last = bounds[0], bounds[-1]
            if len(bounds) > 2 or first < 1 or last < first or last > 5000:
                return value
            pages.update(range(first, last + 1))
        return sorted(pages)
    except (ValueError, IndexError):
        return value


def active_fingerprint(kind: str, payload: dict) -> str:
    """Идентичность включает профиль, вид входа, порядок файлов и все значимые параметры."""
    source = deepcopy(payload)
    if "specification" in source:
        source["specification"]["pages"] = canonical_pages(source["specification"]["pages"])
        mode = "specification-pdf"
    elif "invoice" in source and source["invoice"].get("files"):
        mode = "invoice-files"
        source["invoice"]["page_selection"] = {
            key: canonical_pages(value)
            for key, value in source["invoice"]["page_selection"].items()
        }
    else:
        mode = "json" if "document" in source else "external-job"
    return digest({"kind": kind, "input": mode, "payload": source})


def safe_upstream(value: dict) -> dict:
    """Allowlist метаданных 8083 de7c948; исключает partial, raw_text, IDs и credentials."""
    result = {}
    for key in (
        "status",
        "current_file_index",
        "current_pdf_page",
        "processed_pages",
        "total_pages",
    ):
        item = value.get(key)
        if (
            key == "status"
            and isinstance(item, str)
            and item
            in {
                "queued",
                "running",
                "completed",
                "completed_with_warnings",
                "failed",
                "cancelled",
            }
        ):
            result[key] = item
        elif key != "status" and (
            type(item) is int
            and 0 <= item <= 50000
            or item is None
            and key in {"current_file_index", "current_pdf_page"}
        ):
            result[key] = item
    files = value.get("files")
    if not isinstance(files, list) and "selected_pdf_pages" in value:
        files = [
            {
                "file_index": 0,
                "source_file": value.get("filename"),
                "page_count": value.get("document_pages"),
                "selected_pages": value.get("selected_pdf_pages"),
                "pages": value.get("pages"),
            }
        ]
    result["files"] = []
    for source in (files if isinstance(files, list) else [])[:10]:
        if not isinstance(source, dict):
            continue
        if type(source.get("file_index")) is not int or not 0 <= source["file_index"] < 10:
            continue
        file = {}
        for key in ("file_index", "page_count"):
            if type(source.get(key)) is int and 0 <= source[key] <= 5000:
                file[key] = source[key]
        name = source.get("source_file")
        if isinstance(name, str):
            file["source_file"] = name[:180]
        selected = source.get("selected_pages")
        selected = selected if isinstance(selected, list) else []
        file["selected_pages"] = [p for p in selected[:500] if type(p) is int and 1 <= p <= 5000]
        file["pages"] = []
        pages = source.get("pages")
        for page in (pages if isinstance(pages, list) else [])[:500]:
            if (
                not isinstance(page, dict)
                or type(page.get("pdf_page")) is not int
                or not 1 <= page["pdf_page"] <= 5000
            ):
                continue
            state = page.get("state")
            if not isinstance(state, str) or state not in {
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
            }:
                continue
            stages = page.get("stages")
            stages = [
                item
                for item in (stages if isinstance(stages, list) else [])[:12]
                if isinstance(item, dict)
            ]
            file["pages"].append(
                {
                    "pdf_page": page["pdf_page"],
                    "state": state,
                    "stage": str(stages[-1].get("name", ""))[:64] if stages else None,
                }
            )
        result["files"].append(file)
    return result


def snapshot(job: dict, now: float) -> dict:
    """Возвращает owner-safe metadata без source, upstream UUID, lease и финансовых значений."""
    data, root = job["data"], api_root(job)
    upstream = data.get("upstream_progress", {})
    metadata = data.get("invoice", {}).get("files", [])
    files = upstream.get("files") or [
        {
            "file_index": i,
            "source_file": f["filename"],
            "page_count": f["page_count"],
            "selected_pages": f["selected_pages"],
            "pages": [],
        }
        for i, f in enumerate(metadata)
    ]
    if not files and data.get("specification"):
        spec = data["specification"]
        selected = canonical_pages(spec["pages"])
        files = [
            {
                "file_index": 0,
                "source_file": spec["filename"],
                "selected_pages": selected if isinstance(selected, list) else [],
                "pages": [],
            }
        ]
    source = data.get("source")
    total = (
        (
            source.get("summary", {}).get("item_count")
            if job["kind"] == "invoice"
            else len(source.get("items", []))
        )
        if source
        else None
    )
    started = job.get("started_at")
    processed_pages = upstream.get("processed_pages", 0)
    rows = data.get("rows", [])
    total_pages = upstream.get("total_pages") or sum(
        len(f.get("selected_pages", [])) for f in files
    )
    return {
        "job_id": job["id"],
        "profile": profile(job),
        "api_root": root,
        "status": status(job),
        "stage": job["stage"],
        "revision": job["revision"],
        "catalog_version": data.get("catalog_version"),
        "approved_at": data.get("approval", {}).get("approved_at"),
        "approved_revision": data.get("approval", {}).get("approved_revision"),
        "created_at": job["created_at"],
        "started_at": started,
        "updated_at": job.get("updated_at") or job["created_at"],
        "expires_at": job["expires_at"],
        "elapsed_seconds": max(0, now - (started or job["created_at"])),
        "wait_seconds": max(0, (started or now) - job["created_at"]),
        "last_event_id": job.get("last_event_id", 0),
        "cancel_requested": job.get("cancel_requested", False),
        "error_code": job.get("error_code"),
        "reused": job.get("reused", False),
        "upstream_status": upstream.get("status"),
        "extracting_applicable": data.get("ingestion") != "import-json",
        "processed_pages": processed_pages,
        "total_pages": total_pages or None,
        "current_file_index": upstream.get("current_file_index"),
        "current_pdf_page": upstream.get("current_pdf_page"),
        "files": files,
        "processed_items": len(data.get("rows", [])),
        "counts": dict(Counter(row["status"] for row in rows)),
        "total_items": total,
        "file_count": (
            source.get("summary", {}).get("file_count")
            if source and job["kind"] == "invoice"
            else len(files)
            if files
            else None
        ),
        "document_count": source.get("summary", {}).get("document_count")
        if source and job["kind"] == "invoice"
        else None,
        "matched_count": sum(row["status"] in {"matched", "manual"} for row in rows)
        if source
        else None,
        "unresolved_count": sum(
            row["status"] == "review" or row["status"] == "not_found" and not row["confirmed"]
            for row in rows
        )
        if source
        else None,
        "financial_warning_count": len(financial_warnings(source))
        if source and job["kind"] == "invoice"
        else None,
        "links": {
            "ui": ("/invoices" if job["kind"] == "invoice" else "/mapping") + "?job=" + job["id"],
            "snapshot": root + "/" + job["id"] + "/snapshot",
            "events": root + "/" + job["id"] + "/events",
            "cancel": root + "/" + job["id"] + "/cancel",
        },
    }
