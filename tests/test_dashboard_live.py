# tests/test_dashboard_live.py
"""Реальный HTTP/worker/broker/PG pipeline dashboard только в специальном временном stack."""

import asyncio
from datetime import UTC, datetime, timedelta
from time import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from src.bootstrap import build_container
from src.config import Settings
from src.infrastructure.database import SyncSettingsRow

pytestmark = pytest.mark.live


async def test_live_dashboard_manual_schedule_history_and_reports():
    """Не изменяет глобальные настройки production: требуется явно изолированный тестовый host."""
    settings = Settings()
    if (
        not settings.database_host.startswith("mapper-v2-test-")
        or str(settings.data_directory) != "/app/staging/fixture"
    ):
        pytest.skip("Изменение расписания допустимо только в изолированном V2 test stack")
    container = build_container(settings)
    headers = {"X-Catalog-Key": settings.catalog_admin_key.get_secret_value()}
    fields = {
        "revision",
        "enabled",
        "time_utc",
        "source_type",
        "url",
        "method",
        "auth_type",
        "credential_ref",
        "expected_format",
        "export_mode",
        "timeout_seconds",
        "max_bytes",
        "completion_header",
        "completion_value",
    }
    try:
        async with httpx.AsyncClient(
            base_url=settings.app_public_base_url, headers=headers, timeout=30, trust_env=False
        ) as client:

            async def get(path):
                """Читает реальный HTTP ответ, проверяя статус до использования данных."""
                response = await client.get(path)
                assert response.status_code == 200, response.text
                return response.json()

            async def wait_job(job_id):
                """Ждёт bounded выполнения существующим worker через настоящий RabbitMQ."""
                for _ in range(200):
                    value = await get("/api/v1/catalog/imports/" + job_id)
                    if value["status"] in {"complete", "failed"}:
                        assert value["status"] == "complete", value
                        return value
                    await asyncio.sleep(0.2)
                pytest.fail("Тестовый импорт не завершился вовремя")

            config = await get("/api/v1/catalog/sync-settings")
            body = {key: config[key] for key in fields}
            body.update(enabled=False, source_type="local_xml", time_utc="16:31")
            saved = await client.put("/api/v1/catalog/sync-settings", json=body)
            assert saved.status_code == 200, saved.text
            key = str(uuid4())
            first = await client.post(
                "/api/v1/catalog/imports",
                json={"source_type": "local_xml"},
                headers={"Idempotency-Key": key},
            )
            assert first.status_code == 202, first.text
            repeated = await client.post(
                "/api/v1/catalog/imports",
                json={"source_type": "local_xml"},
                headers={"Idempotency-Key": key},
            )
            assert repeated.json()["job_id"] == first.json()["job_id"]
            done = await wait_job(first.json()["job_id"])
            assert done["processed"] == done["total"] == 3
            report = await get("/api/v1/catalog/import-history/" + done["job_id"] + "/report.json")
            assert report["state"] == "complete"
            xlsx = await client.get(
                "/api/v1/catalog/import-history/" + done["job_id"] + "/report.xlsx"
            )
            assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
            assert (await get("/api/v1/catalog/sync-settings"))["time_utc"] == "16:31"

            config = await get("/api/v1/catalog/sync-settings")
            body = {key: config[key] for key in fields}
            body.update(
                enabled=True, time_utc=(datetime.now(UTC) - timedelta(minutes=1)).strftime("%H:%M")
            )
            assert (await client.put("/api/v1/catalog/sync-settings", json=body)).status_code == 200
            # Только тестовая БД: моделируется один пропущенный слот, без ожидания суток.
            async with container.jobs.sessions.begin() as session:
                await session.execute(
                    update(SyncSettingsRow)
                    .where(SyncSettingsRow.id == 1)
                    .values(next_run_at=time() - 1)
                )
            scheduled = None
            for _ in range(100):
                runs = await get("/api/v1/catalog/import-history?limit=10")
                scheduled = next(
                    (run for run in runs["items"] if run["initiator"].startswith("scheduled")), None
                )
                if scheduled and scheduled["state"] == "complete":
                    break
                await asyncio.sleep(0.2)
            assert scheduled and scheduled["state"] == "complete"
            assert scheduled["details"]["run_statistics"]["embedded_count"] == 0
            config = await get("/api/v1/catalog/sync-settings")
            body = {key: config[key] for key in fields}
            body.update(enabled=False, time_utc="16:31")
            assert (await client.put("/api/v1/catalog/sync-settings", json=body)).status_code == 200
    finally:
        await container.close()
