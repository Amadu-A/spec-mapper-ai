# tests/test_v3_live.py
"""Изолированные live проверки V3: SSH gateway, mock 1С, реальный worker и PG cooldown."""

import asyncio
from time import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine

from src.application.catalog_management import CONFIG_FIELDS
from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import MapperError
from src.infrastructure.database import Base, ServiceActivityRow
from src.presentation.security import COOKIE

pytestmark = pytest.mark.live


def isolated_settings() -> Settings:
    """Не допускает изменения source/cooldown вне специального временного V3 окружения."""
    settings = Settings()
    if not settings.database_host.startswith("mapper-v2-test-v3-"):
        pytest.skip("V3 management live допустим только в изолированном mapper-v2-test-v3-* stack")
    return settings


def configuration(value: dict) -> dict:
    """Передаёт только разрешённые открытые поля и актуальную revision."""
    return {"revision": value["revision"], **{key: value[key] for key in CONFIG_FIELDS}}


async def wait_refresh(client: httpx.AsyncClient, job_id: str) -> dict:
    """Ждёт bounded завершения через реальный HTTP и работающий worker/RabbitMQ."""
    for _ in range(300):
        response = await client.get("/api/v1/catalog/refreshes/" + job_id)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"complete", "failed"}:
            assert result["status"] == "complete", result
            return result
        await asyncio.sleep(0.2)
    pytest.fail("Временный V3 refresh не завершился за отведённое время")


async def test_live_v3_gateway_http_refresh_and_global_cooldown():
    """Проверяет настоящий proxy/auth/SQL/worker; HTTP 1С здесь явный mock полного XML."""
    settings = isolated_settings()
    public_url = settings.app_public_base_url
    api_host = urlsplit(public_url).hostname
    assert api_host and api_host.endswith("-api")
    gateway_url = "http://" + api_host.removesuffix("-api") + "-gateway:8000"
    operator_origin = "http://localhost:18089"
    container = build_container(settings)
    try:
        async with (
            httpx.AsyncClient(
                base_url=gateway_url,
                trust_env=False,
                timeout=30,
                headers={"Host": "localhost:18089"},
            ) as operator,
            httpx.AsyncClient(base_url=public_url, trust_env=False, timeout=30) as first,
            httpx.AsyncClient(base_url=public_url, trust_env=False, timeout=30) as other,
        ):
            csrf = (await operator.get("/api/v1/session")).json()["csrf"]
            operator.headers.update({"Origin": operator_origin, "X-CSRF-Token": csrf})
            session = await operator.get("/api/v1/operator/session")
            assert session.status_code == 200 and session.json()["can_issue_ticket"]
            issued = await operator.post("/api/v1/operator/tickets")
            assert issued.status_code == 201, issued.text
            logged = await operator.post(
                "/api/v1/operator/session", json={"ticket": issued.json()["ticket"]}
            )
            assert logged.status_code == 200 and logged.json()["operator"], logged.text
            operator.headers["X-Operator-CSRF"] = logged.json()["csrf"]

            current = await operator.get("/api/v1/catalog/sync-settings")
            assert current.status_code == 200, current.text
            body = configuration(current.json())
            body.update(
                enabled=False,
                source_type="http_onec",
                url="http://onec-fixture:8000/catalog",
                method="GET",
                auth_type="none",
                expected_format="xml",
                export_mode="full",
                completion_header="X-Export-Complete",
                completion_value="yes",
            )
            saved = await operator.put("/api/v1/catalog/sync-settings", json=body)
            assert saved.status_code == 200, saved.text
            probe = await operator.post("/api/v1/catalog/connection-test")
            assert probe.status_code == 200, probe.text
            assert probe.json()["probe"]["state"] == "confirmed", probe.json()

            for client in (first, other):
                own_csrf = (await client.get("/api/v1/session")).json()["csrf"]
                client.headers.update({"Origin": public_url, "X-CSRF-Token": own_csrf})
            available = await first.get("/api/v1/catalog/refresh-status")
            assert available.status_code == 200 and available.json()["available"], available.text
            denied_settings = await first.put(
                "/api/v1/catalog/sync-settings", json=configuration(probe.json())
            )
            assert denied_settings.status_code == 403
            key = uuid4().hex
            started = await first.post("/api/v1/catalog/refresh", headers={"Idempotency-Key": key})
            assert started.status_code == 202, started.text
            job_id = started.json()["job_id"]
            replay = await first.post("/api/v1/catalog/refresh", headers={"Idempotency-Key": key})
            assert replay.status_code == 202 and replay.json()["job_id"] == job_id
            limited = await other.post(
                "/api/v1/catalog/refresh", headers={"Idempotency-Key": uuid4().hex}
            )
            assert limited.status_code == 429, limited.text
            assert 1 <= int(limited.headers["Retry-After"]) <= 60
            assert (await other.get("/api/v1/catalog/refreshes/" + job_id)).status_code == 404
            completed = await wait_refresh(first, job_id)
            assert completed["processed"] == completed["total"] == 3

            # Только временная БД: эмулируется прошедшая минута, без ожидания/перезапуска worker.
            async with container.jobs.sessions.begin() as db:
                await db.execute(
                    update(ServiceActivityRow)
                    .where(ServiceActivityRow.name == "catalog_manual_refresh")
                    .values(last_seen=time() - 61)
                )
            assert (await first.get("/api/v1/catalog/refresh-status")).json()["available"]
            second = await first.post(
                "/api/v1/catalog/refresh", headers={"Idempotency-Key": uuid4().hex}
            )
            assert second.status_code == 202, second.text
            unchanged = await wait_refresh(first, second.json()["job_id"])
            assert unchanged["metrics"]["embedded_count"] == 0, unchanged

            # Тот же Host на публичном API не является доказательством SSH.
            spoof = await other.post(
                "/api/v1/operator/tickets",
                headers={
                    "Host": "localhost:18089",
                    "Origin": operator_origin,
                    "Cookie": COOKIE + "=" + other.cookies.get(COOKIE),
                },
            )
            assert spoof.status_code == 403
            assert (await operator.delete("/api/v1/operator/session")).status_code == 200
            assert (
                await operator.put("/api/v1/catalog/sync-settings", json=body)
            ).status_code == 403
    finally:
        # Возвращаем совместимый local/OFF профиль только собственного test stack.
        try:
            async with httpx.AsyncClient(
                base_url=public_url,
                trust_env=False,
                timeout=30,
                headers={"X-Catalog-Key": settings.catalog_admin_key.get_secret_value()},
            ) as admin:
                current = await admin.get("/api/v1/catalog/sync-settings")
                if current.status_code == 200:
                    restored = configuration(current.json())
                    restored.update(
                        source_type="local_xml", enabled=False, time_utc="16:31", url=""
                    )
                    result = await admin.put("/api/v1/catalog/sync-settings", json=restored)
                    assert result.status_code == 200, result.text
        finally:
            await container.close()


async def test_live_v3_two_postgresql_refresh_replicas():
    """Проверяет реальный PG enqueue/cooldown в UUID-схеме, без запуска worker pipeline."""
    settings = isolated_settings()
    schema = "mapper_v3_cooldown_" + uuid4().hex
    control = create_async_engine(settings.database_url)
    url = settings.database_url + "?options=" + quote("-csearch_path=" + schema, safe="")
    first = second = None
    created = False
    try:
        async with control.begin() as db:
            await db.execute(text("CREATE SCHEMA " + schema))
            created = True
        first, second = build_container(settings, url), build_container(settings, url)
        async with first.engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        clock = [time()]
        first.jobs.clock = second.jobs.clock = lambda: clock[0]
        results = await asyncio.gather(
            first.sync.refresh("web:first", "replica-first", operator=True),
            second.sync.refresh("web:second", "replica-second", operator=True),
            return_exceptions=True,
        )
        accepted = [result for result in results if isinstance(result, dict)]
        denied = [result for result in results if isinstance(result, MapperError)]
        assert len(accepted) == len(denied) == 1, results
        assert denied[0].status == 429 and denied[0].retry_after == 60
        assert len(await first.jobs.pending()) == 1
        claimed = await first.jobs.claim(accepted[0]["id"])
        assert claimed
        assert await first.jobs.checkpoint(
            claimed["id"], claimed["lease_token"], state="complete", stage="complete"
        )
        clock[0] += 60
        next_job = await second.sync.refresh("web:third", "after-minute", operator=True)
        assert next_job["id"] != accepted[0]["id"]
    finally:
        for container in (first, second):
            if container:
                await container.close()
        if created:
            async with control.begin() as db:
                await db.execute(text("DROP SCHEMA " + schema + " CASCADE"))
        await control.dispose()
