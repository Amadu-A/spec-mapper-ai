# tests/test_dashboard.py
"""API, безопасность и постоянные агрегаты новой главной без внешних live сервисов."""

from io import BytesIO

import httpx
from openpyxl import load_workbook

from src.main import create_app


async def login(client, container):
    """Обменивает настоящий одноразовый SQL ticket через localhost и web CSRF."""
    csrf = (await client.get("/api/v1/session")).json()["csrf"]
    headers = {"Origin": "http://localhost", "X-CSRF-Token": csrf}
    ticket = await container.operator_access.issue()
    response = await client.post(
        "/api/v1/operator/session", json={"ticket": ticket}, headers=headers
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Operator-CSRF": response.json()["csrf"]}, ticket


async def test_real_pages_legacy_links_and_no_keys(system):
    """Настоящие URL переживают reload, прежняя ссылка сохраняет owner и параметры."""
    container, *_ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://localhost"
    ) as client:
        for path in [
            "/",
            "/mapping",
            "/jobs",
            "/catalog",
            "/settings",
            "/integrations",
            "/events",
            "/operator",
            "/help",
            "/integration-guide",
        ]:
            response = await client.get(path)
            assert response.status_code == 200
            assert "b" * 32 not in response.text and "f" * 32 not in response.text
        response = await client.get("/?job=old&source_job_id=source")
        assert response.status_code == 307
        assert response.headers["location"] == "/mapping?job=old&source_job_id=source"


async def test_operator_ticket_csrf_isolation_revocation(system, source):
    """Код одноразовый; cookie не даёт доступ к чужим документам и требует отдельный CSRF."""
    container, *_ = system
    app = create_app(container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        await client.get("/api/v1/session")
        assert (await client.get("/api/v1/catalog/sync-settings")).status_code == 403
        assert (
            await client.get("/api/v1/catalog/sync-settings", headers={"X-API-Key": "a" * 32})
        ).status_code == 403
        headers, ticket = await login(client, container)
        replay = await client.post(
            "/api/v1/operator/session", json={"ticket": ticket}, headers=headers
        )
        assert replay.status_code == 401
        config = (await client.get("/api/v1/catalog/sync-settings")).json()
        body = {
            key: value
            for key, value in config.items()
            if key
            in {
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
        }
        assert (await client.put("/api/v1/catalog/sync-settings", json=body)).status_code == 403
        assert (
            await client.put(
                "/api/v1/catalog/sync-settings",
                json=body,
                headers={**headers, "Origin": "http://evil"},
            )
        ).status_code == 403
        saved = await client.put("/api/v1/catalog/sync-settings", json=body, headers=headers)
        assert saved.status_code == 200
        assert (
            await client.put("/api/v1/catalog/sync-settings", json=body, headers=headers)
        ).status_code == 409
        body["time_utc"] = "25:00"
        assert (
            await client.put("/api/v1/catalog/sync-settings", json=body, headers=headers)
        ).status_code == 422
        foreign = await container.mappings.import_document("api:onec", "foreign", source)
        meta = (await client.get("/api/v1/operator/jobs")).json()["items"][0]
        assert meta["job_id"] == foreign["id"] and not meta["can_open"]
        for suffix in ["", "/result", "/source.json", "/approved.json"]:
            assert (
                await client.get("/api/v1/mappings/" + foreign["id"] + suffix)
            ).status_code == 404
        cookie = client.cookies.get("spec_mapper_operator")
        assert (await client.delete("/api/v1/operator/session", headers=headers)).status_code == 200
        client.cookies.set("spec_mapper_operator", cookie)
        assert (await client.get("/api/v1/catalog/sync-settings")).status_code == 403


async def test_operator_plain_lan_and_expiry(system):
    """LAN HTTP не принимает одноразовый код; срок сессии фиксирован на сервере."""
    container, *_ = system
    clock = [1000.0]
    container.jobs.clock = lambda: clock[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)),
        base_url="http://192.168.55.3:8087",
    ) as client:
        csrf = (await client.get("/api/v1/session")).json()["csrf"]
        ticket = await container.operator_access.issue()
        response = await client.post(
            "/api/v1/operator/session",
            json={"ticket": ticket},
            headers={"Origin": "http://192.168.55.3:8087", "X-CSRF-Token": csrf},
        )
        assert response.status_code == 403
        nonce = await container.operator_access.consume(ticket)
        assert await container.operator_access.valid(nonce)
        clock[0] += container.settings.operator_session_seconds + 1
        assert not await container.operator_access.valid(nonce)


async def test_durable_metrics_manual_null_ttl_and_owner(system, source):
    """KPI переживают удаление source, отделяют manual и неподтверждённый not_found."""
    container, item, *_ = system
    clock = [1793966400.0]  # Фиксированный полдень UTC: тест не зависит от времени запуска.
    container.jobs.clock = lambda: clock[0]
    job = await container.mappings.import_document("owner", "kpi", source)
    await container.processor.process(job["id"])
    before = await container.sync_repository.summary("owner")
    assert before["jobs"] == 1 and before["positions"] == 2
    job = await container.jobs.get(job["id"], "owner")
    job = await container.mappings.select("owner", job["id"], 0, item.guid, job["revision"])
    await container.mappings.select("owner", job["id"], 1, None, job["revision"])
    after = await container.sync_repository.summary("owner")
    assert after["manual"] == 1 and after["unresolved"] == 0
    assert (await container.sync_repository.summary("other"))["positions"] == 0
    assert (await container.sync_repository.summary(None))["jobs_total"] == 1
    assert after["weekly_delta_pp"] is None
    clock[0] += 3601
    await container.jobs.cleanup()
    assert (await container.sync_repository.summary("owner"))["positions"] == 2
    assert (await container.sync_repository.jobs_page("owner"))["total"] == 0
    clock[0] += 86400
    tomorrow = await container.sync_repository.summary("owner")
    assert tomorrow["positions"] == 0 and tomorrow["jobs"] == 0
    assert tomorrow["jobs_total"] == 1


async def test_catalog_reports_survive_job_ttl_and_safe_fields(system):
    """Импорт идёт прежним pipeline; отчёт содержит агрегаты и переживает TTL job."""
    container, *_ = system
    config = await container.sync.settings()
    assert not config["enabled"] and config["time_utc"] == "15:00"
    job = await container.sync.trigger("operator", "report", "local_xml")
    await container.processor.process(job["id"])
    report = await container.sync_repository.report(job["id"])
    assert report["state"] == "complete"
    assert report["details"]["run_statistics"]["added_count"] == 0
    assert "source" not in report["details"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)), base_url="http://localhost"
    ) as client:
        headers = {"X-Catalog-Key": "b" * 32}
        response = await client.get(
            f"/api/v1/catalog/import-history/{job['id']}/report.xlsx", headers=headers
        )
        assert response.status_code == 200
        assert load_workbook(BytesIO(response.content)).active.max_row > 1
    clock = container.jobs.clock() + 86401
    container.jobs.clock = lambda: clock
    await container.jobs.cleanup()
    assert (await container.sync_repository.report(job["id"]))["state"] == "complete"


async def test_http_unknown_contract_cannot_enable_or_leak_secrets(system):
    """Неизвестный HTTP контракт — явное ожидание; секреты не принимаются/не возвращаются."""
    container, *_ = system
    value = await container.sync_repository.settings()
    config = container.sync.config(value)
    config.update(source_type="http_onec", enabled=False)
    await container.sync.save(config, value["revision"], "operator")
    probe = await container.sync.connection_test()
    assert probe["probe"]["state"] == "awaiting_contract"
    assert not probe["is_configured"]
    config["enabled"] = True
    import pytest

    from src.domain.models import MapperError

    with pytest.raises(MapperError):
        await container.sync.save(config, probe["revision"], "operator")
    assert not (await container.sync_repository.settings())["enabled"]
