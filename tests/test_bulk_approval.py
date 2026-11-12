# tests/test_bulk_approval.py
"""Общее подтверждение текущих выборов/null, DELETE, неизменные экспорты и конкуренция CAS."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from io import BytesIO

import httpx
import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from src.domain.models import LEGACY_SOURCE_FIELDS as SOURCE_FIELDS
from src.domain.models import MapperError, confirm_rows
from src.infrastructure.database import ApprovalRow
from src.main import create_app
from src.presentation.schemas import ApprovedExport


def test_bulk_confirmation_preserves_proposals_without_choosing_another_candidate():
    """Подтверждение не вставляет Top-1 вместо пустого выбора и не меняет исходный список."""
    rows = [
        {
            "status": "review",
            "selection": None,
            "confirmed": False,
            "candidates": [{"item": {"guid": "candidate"}, "score": 1.0}],
        },
        {"status": "review", "selection": {"guid": "chosen"}, "confirmed": False, "candidates": []},
        {
            "status": "matched",
            "selection": {"guid": "automatic"},
            "confirmed": False,
            "candidates": [],
        },
    ]
    original = deepcopy(rows)
    confirmed = confirm_rows(rows)
    assert rows == original
    assert [row["status"] for row in confirmed] == ["not_found", "manual", "matched"]
    assert confirmed[0]["selection"] is None
    assert confirmed[1]["selection"] == {"guid": "chosen"}
    assert all(row["confirmed"] and row["manually_reviewed"] for row in confirmed)


async def test_bulk_approval_mixed_states_snapshots_metrics_and_excel(system, source, monkeypatch):
    """Один approve подтверждает авто/review/null атомарно, без повторного обхода строк."""
    container, item, *_ = system
    container.jobs.clock = lambda: 1793966400.0  # Фиксированные UTC-сутки для метрик.
    document = {"items": [deepcopy(source["items"][0]) for _ in range(4)]}
    document["items"][2]["Units"] = "м"
    document["items"][1]["UserComment1"] = "=SUM(A1:A2)"
    original = deepcopy(document)

    async def search(collection, vectors, limit):
        """Подменяет только Qdrant hits: уверенный, слабый, конфликт единиц и пустой Top-K."""
        assert len(vectors) == 4 and limit == 5
        return [[(item.guid, 0.99)], [(item.guid, 0.4)], [(item.guid, 0.99)], []]

    monkeypatch.setattr(container.index, "search", search)
    headers = {"X-API-Key": "a" * 32, "Idempotency-Key": "mixed-bulk"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://test"
    ) as client:
        created = await client.post("/api/v1/mappings/import-json", json=document, headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        await container.processor.process(job_id)
        before = (await client.get(f"/api/v1/mappings/{job_id}", headers=headers)).json()
        assert before["status"] == "ready_for_review" and before["unresolved_count"] == 3
        previous_rows = (await container.jobs.get(job_id))["data"]["rows"]
        assert [row["status"] for row in previous_rows] == [
            "matched",
            "review",
            "review",
            "not_found",
        ]
        metrics_before = await container.sync_repository.summary(None)
        assert metrics_before["matched"] == 1 and metrics_before["manual"] == 0
        assert metrics_before["review"] == 2 and metrics_before["unresolved"] == 3

        response = await client.post(
            f"/api/v1/mappings/{job_id}/approve",
            json={"revision": before["revision"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        approved = response.json()
        assert approved["status"] == "approved" and approved["unresolved_count"] == 0
        assert approved["revision"] == before["revision"]
        assert approved["expires_at"] == before["expires_at"]
        snapshot = (
            await client.get(f"/api/v1/mappings/{job_id}/approved.json", headers=headers)
        ).json()
        ApprovedExport.model_validate(snapshot)
        assert [row["onec"] for row in snapshot["items"]] == [
            row["selection"] for row in previous_rows
        ]
        assert [row["matching"]["status"] for row in snapshot["items"]] == [
            "matched",
            "manual",
            "not_found",
            "not_found",
        ]
        assert all(
            row["matching"]["confirmed"] and row["matching"]["approved"]
            for row in snapshot["items"]
        )
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/source.json", headers=headers)
        ).json() == original
        for actual, expected in zip(snapshot["items"], original["items"], strict=True):
            assert {name: actual[name] for name in SOURCE_FIELDS} == expected
        persisted = await container.jobs.get(job_id)
        assert [row["candidates"] for row in persisted["data"]["rows"]] == [
            row["candidates"] for row in previous_rows
        ]
        assert all(row["confirmed"] for row in persisted["data"]["rows"])
        metrics = await container.sync_repository.summary(None)
        assert metrics["jobs"] == 1 and metrics["positions"] == 4
        assert metrics["matched"] == 1 and metrics["manual"] == 1
        assert metrics["review"] == metrics["unresolved"] == 0

        replay = await client.post(
            f"/api/v1/mappings/{job_id}/approve",
            json={"revision": before["revision"]},
            headers={**headers, "Idempotency-Key": "bulk-replay"},
        )
        assert replay.status_code == 200
        assert (await container.sync_repository.summary(None)) == metrics
        assert (
            await client.get(f"/api/v1/mappings/{job_id}/approved.json", headers=headers)
        ).json() == snapshot
        async with container.jobs.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ApprovalRow)) == 1
        xlsx = await client.get(f"/api/v1/mappings/{job_id}/result.xlsx", headers=headers)
        workbook = load_workbook(BytesIO(xlsx.content))
        values = list(workbook.active.iter_rows(min_row=2, values_only=True))
        for actual, expected in zip(values, original["items"], strict=True):
            assert list(actual[:5]) == [expected[name] for name in SOURCE_FIELDS]
            headers = [cell.value for cell in workbook.active[1]]
            assert actual[headers.index("Проверено человеком")] == "Да"
        assert workbook.active["D3"].data_type == "s"


async def test_delete_selection_cookie_csrf_owner_revision_and_approval(system, source):
    """Крестик очищает только выбор своей строки с CSRF/revision и не меняет исходный JSON."""
    container, *_ = system
    transport = httpx.ASGITransport(app=create_app(container))
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as other,
    ):
        csrf = (await client.get("/api/v1/session")).json()["csrf"]
        headers = {
            "Origin": "http://test",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "delete-cookie",
        }
        created = await client.post("/api/v1/mappings/import-json", json=source, headers=headers)
        job_id = created.json()["job_id"]
        await container.processor.process(job_id)
        before = (await client.get(f"/api/v1/mappings/{job_id}")).json()
        path = f"/api/v1/mappings/{job_id}/rows/0/selection"
        params = {"revision": before["revision"]}
        assert (await client.delete(path, params=params)).status_code == 403
        assert (
            await client.delete(path, params=params, headers={**headers, "Origin": "http://evil"})
        ).status_code == 403
        await other.get("/api/v1/session")
        other_csrf = (await other.get("/api/v1/session")).json()["csrf"]
        assert (
            await other.delete(
                path, params=params, headers={"Origin": "http://test", "X-CSRF-Token": other_csrf}
            )
        ).status_code == 404
        assert (await client.delete(path, headers=headers)).status_code == 422
        response = await client.delete(path, params=params, headers=headers)
        assert response.status_code == 200
        assert response.json()["revision"] == before["revision"] + 1
        assert response.json()["expires_at"] == before["expires_at"]
        page = (await client.get(f"/api/v1/mappings/{job_id}/result")).json()
        first = page["items"][0]
        assert first["selection"] is None and first["status"] == "not_found"
        assert first["confirmed"] and first["manually_reviewed"]
        assert first["candidates"]
        assert (await client.get(f"/api/v1/mappings/{job_id}/source.json")).json() == source
        assert (await client.delete(path, params=params, headers=headers)).status_code == 409
        revision = response.json()["revision"]
        assert (
            await client.post(
                f"/api/v1/mappings/{job_id}/approve", json={"revision": revision}, headers=headers
            )
        ).status_code == 200
        assert (
            await client.delete(path, params={"revision": revision}, headers=headers)
        ).status_code == 409


async def test_bulk_approval_stale_owner_not_ready_and_expired(system, source):
    """Массовое подтверждение сохраняет проверки владельца, revision, готовности и TTL."""
    container, *_ = system
    job = await container.mappings.import_document("owner", "approval-errors", source)
    with pytest.raises(MapperError) as error:
        await container.mappings.approve("owner", job["id"], job["revision"], "approve")
    assert error.value.status == 409
    await container.processor.process(job["id"])
    job = await container.jobs.get(job["id"])
    for owner, revision, expected in [
        ("other", job["revision"], 404),
        ("owner", job["revision"] - 1, 409),
    ]:
        with pytest.raises(MapperError) as error:
            await container.mappings.approve(owner, job["id"], revision, "approve")
        assert error.value.status == expected
    container.jobs.clock = lambda: job["expires_at"] + 1
    with pytest.raises(MapperError) as error:
        await container.mappings.approve("owner", job["id"], job["revision"], "approve")
    assert error.value.status == 410


async def test_concurrent_clear_and_approve_never_mutate_approved_snapshot(system, source):
    """Одна revision даёт одного победителя: clear либо approve, без частичного результата."""
    container, *_ = system
    for index in range(6):
        job = await container.mappings.import_document("owner", f"concurrent-{index}", source)
        await container.processor.process(job["id"])
        job = await container.jobs.get(job["id"])
        results = await asyncio.gather(
            container.mappings.approve("owner", job["id"], job["revision"], f"approve-{index}"),
            container.mappings.select("owner", job["id"], 0, None, job["revision"]),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(successes) == len(errors) == 1, results
        assert isinstance(errors[0], MapperError) and errors[0].status == 409
        actual = await container.jobs.get(job["id"])
        if actual["state"] == "approved":
            snapshot = await container.jobs.approved(job["id"], "owner")
            assert snapshot["items"][0]["onec"] == actual["data"]["rows"][0]["selection"]
            assert actual["data"]["rows"][0]["selection"] is not None
        else:
            assert actual["revision"] == job["revision"] + 1
            assert actual["data"]["rows"][0]["selection"] is None
            with pytest.raises(MapperError) as error:
                await container.jobs.approved(job["id"], "owner")
            assert error.value.code == "not_approved"
