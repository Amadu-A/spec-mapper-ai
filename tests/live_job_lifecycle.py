# tests/live_job_lifecycle.py
"""Реальный Stage 03B 8087→8083/VLM→embedding/Qdrant; только изолированный QA API.

Содержимое 14 разрешённых файлов, финансы и upstream IDs в stdout не выводятся.
"""

import argparse
import asyncio
import json
from pathlib import Path
from time import monotonic

import httpx
from sqlalchemy import select

from src.domain.models import digest
from src.infrastructure.database import JobRow
from tests.live_invoice_cases import checked, key_header, review_and_export, wait_ready
from tests.live_invoice_runtime import checked_settings, live_container


def report(**data):
    """Публикует только счётчики и явные признаки реальной проверки."""
    print(json.dumps(data), flush=True)


async def submit_invoice(client, key, count=2):
    """Передаёт разрешённые реальные PDF без их модификации."""
    paths = await asyncio.to_thread(lambda: sorted(Path("examples/invoices").glob("*.pdf")))
    selected = [paths[0], paths[3]][:count]
    contents = await asyncio.gather(*(asyncio.to_thread(p.read_bytes) for p in selected))
    return checked(
        await client.post(
            "/api/v1/invoices",
            headers={"Idempotency-Key": key},
            files=[
                ("files", (p.name, data, "application/pdf"))
                for p, data in zip(selected, contents, strict=True)
            ],
            data={"page_selection": "{}"},
        ),
        202,
    ).json()["job_id"]


async def snapshot_until(client, root, targets, seconds=600):
    """Наблюдает durable metadata: не запускает отдельный upstream polling."""
    deadline, last = monotonic() + seconds, None
    while monotonic() < deadline:
        value = checked(await client.get(root + "/snapshot")).json()
        marker = (value["status"], value["stage"], value["processed_pages"])
        if marker != last:
            report(
                lifecycle_profile=value["profile"],
                status=value["status"],
                stage=value["stage"],
                processed_pages=value["processed_pages"],
                total_pages=value["total_pages"],
                journal_cursor=value["last_event_id"],
            )
            last = marker
        if value["status"] in targets:
            return value
        assert value["status"] != "failed", value.get("error_code")
        await asyncio.sleep(1)
    raise AssertionError("lifecycle_wait_timeout")


async def flow(client):
    """Оба профиля проходят реальный upstream; exports используют сохранённые snapshots."""
    invoice = await submit_invoice(client, "qa-stage03b-flow")
    path = await asyncio.to_thread(
        lambda: next(Path("examples/invoices").glob("Спецификация*.pdf"))
    )
    content = await asyncio.to_thread(path.read_bytes)
    specification = checked(
        await client.post(
            "/api/v1/specifications",
            headers={"Idempotency-Key": "qa-stage03b-spec"},
            files={"file": (path.name, content, "application/pdf")},
            data={"pages": "1", "project_code": "STAGE03B-QA"},
        ),
        202,
    ).json()["job_id"]
    root = "/api/v1/specifications/" + specification
    await snapshot_until(client, root, {"awaiting_review"})
    ready = await wait_ready(client, root)
    draft = checked(await client.get(root + "/draft.json")).json()
    assert draft["items"]
    checked(
        await client.post(
            root + "/approve",
            json={"revision": ready["revision"]},
            headers={"Idempotency-Key": "qa-stage03b-spec-approve"},
        )
    )
    approved = checked(await client.get(root + "/result.json")).json()
    assert approved["items"] and approved["status"] == "approved"
    xlsx = checked(await client.get(root + "/result.xlsx"))
    assert xlsx.content.startswith(b"PK")
    stream = checked(await client.get(root + "/events?after=0"))
    assert "event: lifecycle" in stream.text and "source_job_id" not in stream.text
    invoice_result = await review_and_export(client, invoice)
    report(
        real_profiles_complete=True,
        specification_items=len(approved["items"]),
        invoice_files=invoice_result["files"],
        invoice_items=invoice_result["items"],
        real_vlm=True,
        real_embedding=True,
        real_qdrant=True,
        terminal_sse=True,
    )


async def cancel(client, container):
    """Отменяет уже принятую managed операцию, не выдавая локальную очередь за VLM cancel."""
    identifier = await submit_invoice(client, "qa-stage03b-cancel", 1)
    root = "/api/v1/invoices/" + identifier
    async with asyncio.timeout(180):
        while True:
            job = await container.jobs.get(identifier)
            if job["data"].get("source_job_id"):
                break
            await asyncio.sleep(0.2)
    before = checked(await client.get(root + "/snapshot")).json()
    response = checked(await client.post(root + "/cancel")).json()
    assert response["status"] in {"cancelling", "cancelled"}
    result = await snapshot_until(client, root, {"cancelled"}, 240)
    assert before["expires_at"] == result["expires_at"]
    after = await container.jobs.get(identifier)
    assert after["data"].get("cancellation_outcome") in {
        "cancelled",
        "completed",
        "completed_with_warnings",
        "failed",
    }
    report(
        real_managed_cancel=True,
        upstream_outcome=after["data"]["cancellation_outcome"],
        ttl_preserved=True,
    )


async def restart_prepare(client, container):
    """Сохраняет hash принятого upstream перед внешним SIGKILL только QA worker."""
    identifier = await submit_invoice(client, "qa-stage03b-restart")
    async with asyncio.timeout(180):
        while True:
            job = await container.jobs.get(identifier)
            if job["data"].get("source_job_id"):
                break
            await asyncio.sleep(0.2)
    value = {"id": identifier, "upstream_hash": digest(job["data"]["source_job_id"])}
    await asyncio.to_thread(Path("/tmp/stage03b-restart.json").write_text, json.dumps(value))
    report(qa_worker_kill_window=True, mapper_job_id=identifier, upstream_binding_durable=True)


async def restart_finish(client, container):
    """Проверяет recovery реального handler после SIGKILL без повторного upstream submit."""
    value = json.loads(await asyncio.to_thread(Path("/tmp/stage03b-restart.json").read_text))
    await snapshot_until(client, "/api/v1/invoices/" + value["id"], {"awaiting_review"}, 600)
    job = await container.jobs.get(value["id"])
    assert digest(job["data"]["source_job_id"]) == value["upstream_hash"]
    result = await review_and_export(client, value["id"])
    async with container.jobs.sessions() as session:
        recoveries = await session.scalar(
            select(JobRow.delivery_recoveries).where(JobRow.id == value["id"])
        )
    assert recoveries >= 1
    report(
        real_worker_sigkill_recovery=True,
        same_upstream=True,
        recoveries=recoveries,
        approved_items=result["items"],
    )


async def run(action):
    """Собирает настоящие production адаптеры в проверенной QA схеме."""
    settings = checked_settings()
    container = live_container(settings)
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8087", headers=key_header(settings), timeout=120
        ) as client:
            if action == "flow":
                await flow(client)
            elif action == "cancel":
                await cancel(client, container)
            elif action == "restart-prepare":
                await restart_prepare(client, container)
            elif action == "restart-finish":
                await restart_finish(client, container)
    finally:
        await container.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["flow", "cancel", "restart-prepare", "restart-finish"])
    asyncio.run(run(parser.parse_args().action))
