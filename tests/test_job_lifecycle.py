# tests/test_job_lifecycle.py
"""Offline API/security/admission/cancel: реальные SQL use-cases, upstream явно подменён."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from src.domain.models import MapperError
from src.infrastructure.database import JobRow
from src.main import create_app

AUTH = {"X-API-Key": "a" * 32, "Idempotency-Key": "lifecycle-first"}


async def test_active_dedup_aliases_survive_completion_and_conflicts(system, source):
    """Разные ключи одного active payload связаны атомарно и сохраняют конфликт после review."""
    container, *_ = system
    results = await asyncio.gather(
        *(container.mappings.import_document("owner", "key-" + str(i), source) for i in range(10))
    )
    assert len({result["id"] for result in results}) == 1
    assert sum(not result["reused"] for result in results) == 1
    async with container.jobs.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(JobRow)) == 1
    identifier = results[0]["id"]
    changed = deepcopy(source)
    changed["items"][0]["Caption"] += " другой"
    with pytest.raises(MapperError) as error:
        await container.mappings.import_document("owner", "key-9", changed)
    assert error.value.code == "idempotency_conflict"
    await container.processor.process(identifier)
    assert (await container.mappings.import_document("owner", "key-9", source))["id"] == identifier
    assert (await container.mappings.import_document("owner", "new-explicit", source))[
        "id"
    ] != identifier
    assert (await container.mappings.import_document("other-owner", "key-9", source))[
        "id"
    ] != identifier


async def test_canonical_spec_pages_and_admission_do_not_lose_existing_job(system, source):
    """Технически одинаковые страницы reused; cap не запрещает retry уже принятого payload."""
    container, *_ = system
    first = await container.specifications.submit(
        "owner", "pdf-first", b"%PDF-safe", "one.pdf", "1-2", "P1"
    )
    again = await container.specifications.submit(
        "owner", "pdf-second", b"%PDF-safe", "one.pdf", "1, 2", "P1"
    )
    assert again["id"] == first["id"] and again["reused"]
    container.settings.job_owner_active_limit = 1
    with pytest.raises(MapperError) as error:
        await container.mappings.import_document("owner", "different-input", source)
    assert error.value.status == 429 and error.value.retry_after == 30
    assert (
        await container.specifications.submit(
            "owner", "pdf-third", b"%PDF-safe", "one.pdf", "1-2", "P1"
        )
    )["id"] == first["id"]
    admin = await container.jobs.create("system", "admin", {}, "catalog")
    assert admin["state"] == "queued"


async def test_snapshot_sse_owner_ttl_and_no_source_leak(system, source):
    """История/snapshot/replay безопасны; terminal SSE закрывается и освобождает slot."""
    container, *_ = system
    app = create_app(container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/mappings/import-json", headers=AUTH, json=source)
        assert response.status_code == 202
        root = "/api/v1/mappings/" + response.json()["job_id"]
        view = (await client.get(root + "/snapshot", headers=AUTH)).json()
        assert view["extracting_applicable"] is False and view["total_items"] == 2
        assert view["total_pages"] is None and view["processed_pages"] == 0
        history = (await client.get("/api/v1/specifications", headers=AUTH)).json()
        assert history["total"] == 1 and history["items"][0]["links"]["ui"].startswith(
            "/mapping?job="
        )
        await container.processor.process(response.json()["job_id"])
        ready = (await client.get(root + "/snapshot", headers=AUTH)).json()
        stream = await client.get(root + "/events?after=0", headers=AUTH)
        assert stream.status_code == 200 and "text/event-stream" in stream.headers["content-type"]
        records = [
            json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
        ]
        assert records and [r["id"] for r in records] == sorted({r["id"] for r in records})
        assert records[-1]["id"] == ready["last_event_id"]
        assert source["items"][0]["Caption"] not in stream.text
        assert not app.state.lifecycle_streams
        replay = await client.get(
            root + "/events?after=0", headers={**AUTH, "Last-Event-ID": str(records[0]["id"])}
        )
        assert f"id: {records[0]['id']}\n" not in replay.text
        assert (await client.get(root + "/events?after=-1", headers=AUTH)).status_code == 409
        assert (
            await client.get(root.replace("mappings", "invoices") + "/snapshot", headers=AUTH)
        ).status_code == 404
        await client.get("/mapping")
        assert (await client.get(root + "/snapshot")).status_code == 404
        assert (await client.post(root + "/cancel")).status_code == 403
        container.jobs.clock = lambda: ready["expires_at"] + 1
        for suffix in ["/snapshot", "/events", "/cancel"]:
            method = client.post if suffix == "/cancel" else client.get
            assert (await method(root + suffix, headers=AUTH)).status_code == 410
        await container.jobs.cleanup()
        assert (await client.get(root + "/snapshot", headers=AUTH)).status_code == 404


async def test_bounded_events_do_not_change_review_revision(system, source):
    """Cursor монотонен, журнал ограничен, потерянный replay требует snapshot, CAS неизменен."""
    container, *_ = system
    container.settings.lifecycle_event_limit = 20
    job = await container.mappings.import_document("owner", "bounded", source)
    claimed = await container.jobs.claim(job["id"])
    for index in range(30):
        assert await container.jobs.checkpoint(
            job["id"], claimed["lease_token"], state="polling", stage="stage-" + str(index)
        )
    view = await container.jobs.lifecycle_view("owner", job["id"])
    assert view["revision"] == 0 and view["last_event_id"] > 30
    with pytest.raises(MapperError) as error:
        await container.jobs.lifecycle_events("owner", job["id"], 0)
    assert error.value.code == "event_cursor_expired"
    assert (
        len(await container.jobs.lifecycle_events("owner", job["id"], view["last_event_id"] - 20))
        == 20
    )
    cancelled = await container.jobs.cancel("owner", job["id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["expires_at"] == job["expires_at"]
    assert not await container.jobs.checkpoint(job["id"], claimed["lease_token"], state="complete")


class ManagedUpstream:
    """Контрактный double Stage 03A с управляемым окном POST/ответа и явной отменой."""

    def __init__(self, fail=False):
        """Сохраняет только synthetic UUID и счётчики вызовов."""
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.fail, self.starts, self.cancels = fail, 0, 0
        self.identifier = str(uuid4())

    async def upload(self, content, filename):
        """Оригинал не читается; fake upload не является реальной интеграцией."""
        return str(uuid4())

    async def start(self, *args):
        """Имитирует неопределённую передачу или принятый POST до durable записи ID."""
        self.starts += 1
        self.entered.set()
        if self.fail:
            raise MapperError("upstream_timeout", "Потерян ответ", 503)
        await self.release.wait()
        return self.identifier

    async def fetch(self, *args):
        """После cancel normal pipeline не должен получить исходный JSON."""
        raise AssertionError("cancelled pipeline must not fetch")

    async def cancel(self, identifier):
        """Первый ответ ещё running, следующий доказывает остановку принятой операции."""
        assert identifier == self.identifier
        self.cancels += 1
        return {"status": "cancelled" if self.cancels > 1 else "running", "cancel_requested": True}


async def test_cancel_during_managed_post_preserves_late_id_and_durable_obligation(system):
    """Cancel не теряет поздний ID и не выдаёт running upstream за подтверждённую отмену."""
    container, *_ = system
    upstream = ManagedUpstream()
    container.processor.extraction = upstream
    job = await container.specifications.submit(
        "owner", "managed-race", b"%PDF-safe", "one.pdf", "1", "P1"
    )
    task = asyncio.create_task(container.processor.process(job["id"]))
    await asyncio.wait_for(upstream.entered.wait(), 2)
    cancelled = await container.jobs.cancel("owner", job["id"])
    assert cancelled["state"] == "cancelling"
    upstream.release.set()
    await task
    assert (await container.jobs.get(job["id"]))["data"]["source_job_id"] == upstream.identifier
    await container.processor.process(job["id"])
    assert (await container.jobs.get(job["id"]))["state"] == "cancelling"
    await container.processor.process(job["id"])
    final = await container.jobs.get(job["id"])
    assert final["state"] == "cancelled" and final["expires_at"] == job["expires_at"]
    assert upstream.starts == 1 and upstream.cancels == 2
    assert upstream.identifier not in json.dumps(
        await container.jobs.lifecycle_view("owner", job["id"])
    )


async def test_cancel_uncertain_post_does_not_create_upstream_for_cancellation(system):
    """Без lookup неизвестный ответ остаётся durable cancelling без нового POST."""
    container, *_ = system
    upstream = ManagedUpstream(fail=True)
    container.processor.extraction = upstream
    job = await container.specifications.submit(
        "owner", "uncertain", b"%PDF-safe", "one.pdf", "1", "P1"
    )
    await container.processor.process(job["id"])
    await container.jobs.cancel("owner", job["id"])
    await container.processor.process(job["id"])
    current = await container.jobs.get(job["id"])
    assert (
        current["state"] == "cancelling" and current["error_code"] == "upstream_submission_unknown"
    )
    assert upstream.starts == 1 and upstream.cancels == 0


async def test_cancel_external_and_approve_race_keeps_snapshot_immutable(system, source):
    """External не отменяется в 8083; approve/cancel сериализованы и не уничтожают snapshot."""
    container, *_ = system
    external = await container.mappings.submit("owner", "external", str(uuid4()))
    assert (await container.jobs.cancel("owner", external["id"]))["state"] == "cancelled"
    for index in range(4):
        job = await container.mappings.import_document("owner", "race-" + str(index), source)
        await container.processor.process(job["id"])
        ready = await container.jobs.get(job["id"])
        outcomes = await asyncio.gather(
            container.mappings.approve("owner", job["id"], ready["revision"], "approve"),
            container.jobs.cancel("owner", job["id"]),
            return_exceptions=True,
        )
        final = await container.jobs.get(job["id"])
        assert final["state"] in {"approved", "cancelled"}
        assert any(isinstance(value, MapperError) for value in outcomes)
        if final["state"] == "approved":
            assert (await container.jobs.approved(job["id"], "owner"))["status"] == "approved"


async def test_stream_limit_retry_after_and_openapi_contract(system, source):
    """Лимит SSE проверяется до headers; схемы трёх профилей не меняют JSON exports."""
    container, *_ = system
    app = create_app(container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/mappings/import-json", headers=AUTH, json=source)
        identifier = response.json()["job_id"]
        job = await container.jobs.get(identifier)
        app.state.lifecycle_streams[job["owner"]] = container.settings.lifecycle_sse_per_owner
        limited = await client.get(f"/api/v1/mappings/{identifier}/events", headers=AUTH)
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "15"
        app.state.lifecycle_streams.clear()
        schema = (await client.get("/api/openapi.json")).json()
        for family in ["mappings", "specifications", "invoices"]:
            for suffix in ["snapshot", "events", "cancel"]:
                assert f"/api/v1/{family}/{{job_id}}/{suffix}" in schema["paths"]
        assert schema["paths"]["/api/v1/invoices/{job_id}/events"]["get"]["responses"]["200"][
            "content"
        ].get("text/event-stream")


async def test_cancel_before_claim_does_not_call_upstream_or_matching(system, source):
    """Queued cancel не открывает extraction/embedding и не меняет TTL."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "cancel-queued", source)
    before = await container.jobs.cancel("owner", job["id"])
    assert await container.processor.process(job["id"]) == "noop"
    after = await container.jobs.get(job["id"])
    assert after["state"] == "cancelled" and after["attempts"] == 0
    assert after["expires_at"] == before["expires_at"]


def test_upstream_metadata_allowlist_keeps_selected_physical_pages_only():
    """Контракт Stage 03A: нет partial/secret/UUID, malformed metadata не ломает snapshot."""
    from src.domain.lifecycle import safe_upstream
    from src.presentation.lifecycle_schemas import FileProgress

    data = safe_upstream(
        {
            "status": "running",
            "processed_pages": 1,
            "total_pages": 3,
            "source_job_id": str(uuid4()),
            "raw_text": "PRIVATE",
            "api_key": "SECRET",
            "files": [
                {
                    "file_index": 0,
                    "source_file": "safe.pdf",
                    "page_count": 10,
                    "selected_pages": [2, 4, 9],
                    "pages": [
                        {
                            "pdf_page": 2,
                            "state": "completed",
                            "raw_text": "PRIVATE",
                            "stages": [{"name": "recognition", "partial": "PRIVATE"}],
                        },
                        {"pdf_page": -1, "state": "running"},
                    ],
                },
                {"source_file": "missing-index"},
            ],
        }
    )
    assert "PRIVATE" not in json.dumps(data) and "SECRET" not in json.dumps(data)
    assert len(data["files"]) == 1
    file = FileProgress.model_validate(data["files"][0])
    assert file.selected_pages == [2, 4, 9] and file.pages[0].stage == "recognition"
    assert len(file.pages) == 1
    assert safe_upstream({"status": [], "files": [None, {}]}) == {
        "current_file_index": None,
        "current_pdf_page": None,
        "files": [],
    }
