# tests/live_invoice_cases.py
"""Реальные invoice QA-сценарии без model doubles; выполняются только во временном API контейнере.

Каталог читается из production read-only; jobs/approvals/blobs — в QA schema.
Содержимое счетов, реквизиты сторон, API-ключи и upstream ID не печатаются.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from time import monotonic, time
from uuid import uuid4

import httpx
from openpyxl import load_workbook
from sqlalchemy import func, select, text, update

from src.domain.models import digest
from src.infrastructure.database import InvoiceUploadRow, JobRow
from tests.live_invoice_runtime import checked_settings, live_container


class CaseFailure(Exception):
    """Технический отказ проверки без содержимого HTTP-ответа или исходного документа."""


def key_header(settings) -> dict[str, str]:
    """Берёт существующий server-only ключ через Settings, не создавая новых credentials."""
    key = settings.api_keys.get("onec")
    if key is None:
        key = settings.api_key
    if not key.get_secret_value():
        raise CaseFailure("integration_key_missing")
    return {"X-API-Key": key.get_secret_value()}


def checked(response: httpx.Response, status: int = 200) -> httpx.Response:
    """Сохраняет только безопасный error_code; raw JSON с реквизитами не печатается."""
    if response.status_code != status:
        try:
            code = response.json().get("error", {}).get("code", "http_error")
        except (ValueError, AttributeError):
            code = "http_error"
        raise CaseFailure(str(response.status_code) + ":" + code)
    return response


async def wait_ready(client: httpx.AsyncClient, root: str, deadline_seconds=3400) -> dict:
    """Ожидает реальный durable процесс, выдавая только status/counts раз в 30 секунд."""
    deadline, reported = monotonic() + deadline_seconds, 0.0
    while monotonic() < deadline:
        result = checked(await client.get(root)).json()
        if result["status"] in {"awaiting_review", "approved"}:
            return result
        if result["status"] == "failed":
            raise CaseFailure(result.get("error_code") or "job_failed")
        if monotonic() - reported > 30:
            print(
                json.dumps(
                    {
                        "live_status": result["status"],
                        "processed": result["processed"],
                        "total": result["total"],
                    }
                ),
                flush=True,
            )
            reported = monotonic()
        await asyncio.sleep(2)
    raise CaseFailure("live_wait_timeout")


async def review_and_export(
    client: httpx.AsyncClient, identifier: str, expected: dict | None = None
) -> dict:
    """Проверяет реальные Top-5/поиск/CAS/null/finance guard и неизменяемые JSON/Excel."""
    root = "/api/v1/invoices/" + identifier
    status = await wait_ready(client, root)
    source = checked(await client.get(root + "/source.json")).json()
    if expected is not None and digest(source) != digest(expected):
        raise CaseFailure("source_changed")
    rows = checked(await client.get(root + "/rows", params={"limit": 100})).json()
    revision = rows["revision"]
    eligible = [row for row in rows["items"] if row["selectable"]]
    with_candidates = [row for row in eligible if row["candidates"]]
    if status["status"] != "approved":
        if (await client.get(root + "/result.json")).status_code != 409 or (
            await client.get(root + "/result.xlsx")
        ).status_code != 409:
            raise CaseFailure("unapproved_export_allowed")
        if with_candidates:
            row = with_candidates[0]
            candidates = checked(
                await client.get(root + "/candidates", params={"row_index": row["index"]})
            ).json()
            if not 1 <= len(candidates["candidates"]) <= 5:
                raise CaseFailure("invalid_top_k")
            item = row["candidates"][0]["item"]
            found = checked(
                await client.get("/api/v1/catalog/items", params={"q": item["code"], "limit": 50})
            ).json()["items"]
            if not any(position["guid"] == item["guid"] for position in found):
                raise CaseFailure("catalog_search_mismatch")
            changed = checked(
                await client.put(
                    root + f"/rows/{row['index']}/selection",
                    json={"catalog_item_guid": item["guid"], "revision": revision},
                )
            ).json()
            revision = changed["revision"]
        if len(eligible) > 1:
            row = eligible[1]
            changed = checked(
                await client.delete(
                    root + f"/rows/{row['index']}/selection", params={"revision": revision}
                )
            ).json()
            revision = changed["revision"]
        acknowledged = [warning["document_id"] for warning in rows["financial_warnings"]]
        if (
            acknowledged
            and (
                await client.post(
                    root + "/approve",
                    json={"revision": revision},
                    headers={"Idempotency-Key": "qa-no-financial-ack"},
                )
            ).status_code
            != 409
        ):
            raise CaseFailure("financial_guard_missing")
        checked(
            await client.post(
                root + "/approve",
                json={"revision": revision, "acknowledged_document_warnings": acknowledged},
                headers={"Idempotency-Key": "qa-approve"},
            )
        )
    snapshot = checked(await client.get(root + "/result.json")).json()
    for file, original in zip(snapshot["files"], source["files"], strict=True):
        for document, original_doc in zip(file["documents"], original["documents"], strict=True):
            copy = deepcopy(document)
            for name in ["document_index", "matching_summary", "ready_for_onec"]:
                copy.pop(name)
            for item in copy["items"]:
                item.pop("onec")
                item.pop("matching")
            if digest(copy) != digest(original_doc):
                raise CaseFailure("invoice_finances_or_order_changed")
    book = load_workbook(
        BytesIO(checked(await client.get(root + "/result.xlsx")).content), read_only=True
    )
    if book.sheetnames != ["Документы", "Позиции", "Предупреждения"]:
        raise CaseFailure("excel_sheets_mismatch")
    count = sum(1 for _ in book["Позиции"].iter_rows(min_row=2))
    book.close()
    if count != source["summary"]["item_count"]:
        raise CaseFailure("excel_row_count_mismatch")
    if digest(checked(await client.get(root + "/source.json")).json()) != digest(source):
        raise CaseFailure("approved_source_changed")
    matches = [
        item["onec"]
        for file in snapshot["files"]
        for doc in file["documents"]
        for item in doc["items"]
        if item["onec"]
    ]
    return {
        "files": source["summary"]["file_count"],
        "documents": source["summary"]["document_count"],
        "items": count,
        "rows_with_candidates": len(with_candidates),
        "candidate_count": sum(len(row["candidates"]) for row in rows["items"]),
        "candidate_conflicts": dict(
            Counter(
                reason
                for row in rows["items"]
                for candidate in row["candidates"]
                for reason in candidate.get("conflicts", [])
            )
        ),
        "selections_with_real_guid": sum(bool(item.get("onec_guid")) for item in matches),
        "financial_warning_documents": len(rows["financial_warnings"]),
        "catalog_version": rows["catalog_version"],
    }


async def corpus(client: httpx.AsyncClient, folder: Path) -> None:
    """Сопоставляет реальными embeddings/Qdrant все семь готовых JSON без вызова VLM."""
    paths = await asyncio.to_thread(lambda: sorted(folder.glob("*.json")))
    if len(paths) != 7:
        raise CaseFailure("seven_corpus_json_required")
    total = 0
    for index, path in enumerate(paths):
        source = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8-sig"))
        response = checked(
            await client.post(
                "/api/v1/invoices/import-json",
                json=source,
                headers={"Idempotency-Key": f"qa-corpus-{index}"},
            ),
            202,
        ).json()
        result = await review_and_export(client, response["job_id"], source)
        total += result["items"]
        print(json.dumps({"real_corpus_case": index, **result}), flush=True)
    if total != 161:
        raise CaseFailure("corpus_count_mismatch")
    print(
        json.dumps({"real_corpus_complete": True, "files": 7, "documents": 7, "items": total}),
        flush=True,
    )


async def vlm(client: httpx.AsyncClient, folder: Path) -> None:
    """Выполняет настоящий multipart 8087→AMQP→8083/VLM→embedding/Qdrant→review/export."""
    paths = await asyncio.to_thread(lambda: sorted(folder.glob("*.pdf")))
    chosen = [paths[0], paths[3]]
    start = monotonic()
    contents = await asyncio.gather(*(asyncio.to_thread(path.read_bytes) for path in chosen))
    response = checked(
        await client.post(
            "/api/v1/invoices",
            files=[
                ("files", (path.name, content, "application/pdf"))
                for path, content in zip(chosen, contents, strict=True)
            ],
            data={"page_selection": "{}"},
            headers={"Idempotency-Key": "qa-real-vlm-two-files"},
        ),
        202,
    ).json()
    result = await review_and_export(client, response["job_id"])
    print(
        json.dumps(
            {"real_vlm_complete": True, "elapsed_seconds": round(monotonic() - start, 2), **result}
        ),
        flush=True,
    )


def api_memory() -> dict:
    """Читает RSS/HWM только собственного API процесса внутри PID namespace QA контейнера."""
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():
            continue
        try:
            command = (folder / "cmdline").read_bytes()
            executable = command.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1]
            if (
                not executable.startswith(b"python")
                or b"tests.live_invoice_runtime\x00api" not in command
            ):
                continue
            values = {
                line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                for line in (folder / "status").read_text().splitlines()
                if ":" in line
            }
            return {
                "rss_kib": int(values["VmRSS"].split()[0]),
                "high_water_kib": int(values["VmHWM"].split()[0]),
            }
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    raise CaseFailure("qa_api_memory_unavailable")


async def benchmark(client: httpx.AsyncClient, folder: Path, settings) -> None:
    """Измеряет durable admission десяти реальных PDF при остановленном только QA worker."""
    paths = await asyncio.to_thread(lambda: sorted(folder.glob("*.pdf")))
    chosen = [paths[index % len(paths)] for index in range(10)]
    contents = await asyncio.gather(*(asyncio.to_thread(path.read_bytes) for path in chosen))
    content = [(path.name, body) for path, body in zip(chosen, contents, strict=True)]
    before = await asyncio.to_thread(api_memory)
    start = monotonic()
    response = checked(
        await client.post(
            "/api/v1/invoices",
            files=[("files", (name, data, "application/pdf")) for name, data in content],
            headers={"Idempotency-Key": "qa-ten-file-admission-" + uuid4().hex},
        ),
        202,
    ).json()
    elapsed = monotonic() - start
    container = live_container(settings)
    try:
        async with container.jobs.sessions() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
            if schema != settings.qa_schema:
                raise CaseFailure("qa_schema_mismatch")
            result = await session.execute(
                select(
                    func.sum(func.octet_length(InvoiceUploadRow.content)),
                    func.sum(func.pg_column_size(InvoiceUploadRow.content)),
                ).where(InvoiceUploadRow.job_id == response["job_id"])
            )
            stored, physical = result.one()
        status = checked(await client.get("/api/v1/invoices/" + response["job_id"])).json()
        if status["status"] != "queued" or status["file_count"] != 10:
            raise CaseFailure("qa_worker_must_be_stopped_before_benchmark")
        print(
            json.dumps(
                {
                    "ten_file_admission": True,
                    "files": 10,
                    "uploaded_bytes": sum(len(data) for _, data in content),
                    "stored_blob_bytes": stored,
                    "postgresql_blob_bytes": physical,
                    "elapsed_seconds": round(elapsed, 3),
                    "files_per_second": round(10 / elapsed, 2),
                    "memory_before": before,
                    "memory_after": await asyncio.to_thread(api_memory),
                }
            ),
            flush=True,
        )
    finally:
        await container.close()


async def contracts(client: httpx.AsyncClient, folder: Path, settings) -> None:
    """Проверяет реальные HTTP/OpenAPI/auth/owner/preview/TTL только собственных QA jobs."""
    assert checked(await client.get("/health/live")).json()["live"]
    schema = checked(await client.get("/api/openapi.json")).json()
    assert "/api/v1/invoices" in schema["paths"]
    assert "/api/v1/specifications" in schema["paths"]
    assert "swagger-ui-bundle.js" in checked(await client.get("/docs")).text
    assert len(checked(await client.get("/static/swagger/swagger-ui-bundle.js")).content) > 100000
    paths = await asyncio.to_thread(lambda: sorted(folder.glob("*.pdf")))
    chosen = [paths[0], paths[3]]
    contents = await asyncio.gather(*(asyncio.to_thread(path.read_bytes) for path in chosen))
    job = checked(
        await client.post(
            "/api/v1/invoices",
            files=[
                ("files", (path.name, content, "application/pdf"))
                for path, content in zip(chosen, contents, strict=True)
            ],
            data={"page_selection": "{}"},
            headers={"Idempotency-Key": "qa-real-vlm-two-files"},
        ),
        202,
    ).json()
    root = "/api/v1/invoices/" + job["job_id"]
    assert (await client.get(root, headers={"X-API-Key": "invalid-key"})).status_code == 401
    async with httpx.AsyncClient(base_url=str(client.base_url)) as other:
        assert (await other.get(root)).status_code == 401
        checked(await other.get("/invoices"))
        assert (await other.get(root)).status_code == 404
        assert (await other.get(root + "/result.xlsx")).status_code == 404
    preview = checked(await client.get(root + "/files/0/pages/1/preview", params={"width": 240}))
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
    source_path = await asyncio.to_thread(lambda: sorted(folder.glob("*.json"))[0])
    source = json.loads(await asyncio.to_thread(source_path.read_text, encoding="utf-8-sig"))
    expiring = checked(
        await client.post(
            "/api/v1/invoices/import-json",
            json=source,
            headers={"Idempotency-Key": "qa-expiry-" + uuid4().hex},
        ),
        202,
    ).json()
    container = live_container(settings)
    try:
        async with container.jobs.sessions.begin() as session:
            assert await session.scalar(text("SELECT current_schema()")) == settings.qa_schema
            await session.execute(
                update(JobRow)
                .where(JobRow.id == expiring["job_id"])
                .values(
                    expires_at=time() - 1,
                )
            )
        expired_root = "/api/v1/invoices/" + expiring["job_id"]
        for suffix in ["", "/rows", "/source.json", "/draft.json", "/result.json", "/result.xlsx"]:
            assert (await client.get(expired_root + suffix)).status_code == 410
        print(
            json.dumps(
                {
                    "live_http_contracts": True,
                    "openapi": True,
                    "swagger_assets": True,
                    "auth_401": True,
                    "owner_404": True,
                    "ttl_410": True,
                    "real_preview_proxy": True,
                }
            ),
            flush=True,
        )
    finally:
        await container.close()


async def run(action: str) -> None:
    """Запускает явно выбранный реальный сценарий без вывода персональных/финансовых данных."""
    settings = checked_settings()
    folder = Path("examples/invoices")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8087", headers=key_header(settings), timeout=120
    ) as client:
        if action == "corpus":
            await corpus(client, folder)
        elif action == "vlm":
            await vlm(client, folder)
        elif action == "benchmark":
            await benchmark(client, folder, settings)
        elif action == "contracts":
            await contracts(client, folder, settings)
        else:
            raise CaseFailure("unknown_case")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["corpus", "vlm", "benchmark", "contracts"])
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.action))
    except Exception as error:
        print(
            json.dumps(
                {
                    "real_case_failed": type(error).__name__,
                    "code": str(error) if isinstance(error, CaseFailure) else "internal_test_error",
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
