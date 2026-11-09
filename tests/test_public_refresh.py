# tests/test_public_refresh.py
"""Обычный web/API запуск подтверждённого HTTP источника с общим SQL минутным интервалом."""

import asyncio

import httpx
import pytest

from src.application.catalog_management import CatalogManagement
from src.domain.models import MapperError
from src.infrastructure.sync_repository import SqlSyncRepository
from src.main import create_app


async def configure_http(container):
    """Записывает mock-подтверждённый профиль; не изображает реальный HTTP probe 1С."""
    container.settings.onec_catalog_contract_confirmed = True
    container.settings.onec_catalog_allowed_hosts = ["onec.test"]
    value = await container.sync_repository.settings()
    config = container.sync.config(value)
    config.update(
        source_type="http_onec",
        url="https://onec.test/catalog",
        completion_header="X-Full",
        completion_value="yes",
    )
    await container.sync.save(config, value["revision"], "operator")
    value = await container.sync_repository.settings()
    await container.sync_repository.probe(
        value["revision"],
        {"state": "confirmed", "source_identity": container.catalog_sources.identity(value)},
    )


async def test_public_refresh_source_permission_cooldown_replay_and_no_secret(system, monkeypatch):
    """Минутный лимит общий для owners, переживает новый repository и не продлевается при replay."""
    container, *_ = system
    clock = [1793966400.0]
    container.jobs.clock = lambda: clock[0]
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)), base_url="http://localhost"
        ) as first,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)), base_url="http://localhost"
        ) as second,
    ):
        for client in [first, second]:
            await client.get("/api/v1/session")
        status = (await first.get("/api/v1/catalog/refresh-status")).json()
        assert not status["available"] and status["reason"] == "operator_required"
        key = {"X-API-Key": "a" * 32, "Idempotency-Key": "public"}
        assert (
            await first.post("/api/v1/catalog/refresh", json={}, headers=key)
        ).status_code == 403
        await configure_http(container)
        status = await first.get("/api/v1/catalog/refresh-status")
        assert status.json()["available"]
        assert "onec.test" not in status.text and "_source_identity" not in status.text
        # Фиктивный внешний source только здесь; HTTP safety имеет отдельные adapter tests.
        monkeypatch.setattr(
            container.catalog_sources,
            "source",
            lambda config, progress: container.catalog_sources.local,
        )
        csrf = (await first.get("/api/v1/session")).json()["csrf"]
        headers = {
            "Origin": "http://localhost",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "browser-refresh",
        }
        assert (await first.post("/api/v1/catalog/refresh", json={})).status_code == 403
        assert (
            await first.post(
                "/api/v1/catalog/refresh", headers=headers, json={"url": "https://evil.test/"}
            )
        ).status_code == 422
        created = await first.post("/api/v1/catalog/refresh", headers=headers, json={})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        assert (await first.post("/api/v1/catalog/refresh", headers=headers, json={})).json()[
            "job_id"
        ] == job_id
        assert (await second.get("/api/v1/catalog/refreshes/" + job_id)).status_code == 404
        await container.processor.process(job_id)
        assert (await first.get("/api/v1/catalog/refreshes/" + job_id)).json()[
            "status"
        ] == "complete"
        blocked = await second.post("/api/v1/catalog/refresh", json={}, headers=key)
        assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"
        assert blocked.json()["error"]["retry_after"] == 60
        assert (await first.get("/api/v1/catalog/refresh-status")).json()["retry_after"] == 60
        replica = SqlSyncRepository(container.jobs.sessions, container.settings, container.jobs)
        service = CatalogManagement(
            replica, container.catalog_sources, container.settings, lambda: clock[0]
        )
        clock[0] += 59.9
        with pytest.raises(MapperError) as error:
            await service.refresh("other-owner", "replica")
        assert error.value.status == 429 and error.value.retry_after == 1
        clock[0] += 0.1
        assert (await service.refresh_status())["available"]
        again = await second.post("/api/v1/catalog/refresh", json={}, headers=key)
        assert again.status_code == 202
        assert again.json()["job_id"] != job_id


async def test_public_refresh_concurrent_owners_one_job_and_disabled_contract(system):
    """Параллельные owners не создают дубликатов и не включают источник без контракта."""
    container, *_ = system
    await configure_http(container)
    results = await asyncio.gather(
        *(container.sync.refresh("owner" + str(i), "key" + str(i)) for i in range(4)),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in results) == 1
    assert all(
        isinstance(value, dict) or isinstance(value, MapperError) and value.status == 429
        for value in results
    )
    assert len(await container.jobs.pending()) == 1
    container.settings.onec_catalog_contract_confirmed = False
    assert not (await container.sync.refresh_status())["available"]
    with pytest.raises(MapperError):
        await container.sync.refresh("another", "disabled")


async def test_public_refresh_source_revision_race_and_replay(system, monkeypatch):
    """Смена источника до enqueue отклоняет новый запрос, но не меняет результат принятого."""
    container, *_ = system
    await configure_http(container)
    original = container.sync_repository.trigger

    async def changed(*args, **kwargs):
        """Меняет настройки после проверки HTTP профиля, перед атомарной записью outbox."""
        value = await container.sync_repository.settings()
        config = container.sync.config(value)
        config.update(source_type="local_xml", enabled=False)
        await container.sync.save(config, value["revision"], "operator")
        return await original(*args, **kwargs)

    monkeypatch.setattr(container.sync_repository, "trigger", changed)
    with pytest.raises(MapperError) as error:
        await container.sync.refresh("owner", "race")
    assert error.value.code == "revision_conflict"
    assert await container.jobs.pending() == []
    assert (await container.sync_repository.refresh_status())["retry_after"] == 0
    monkeypatch.setattr(container.sync_repository, "trigger", original)
    await configure_http(container)
    accepted = await container.sync.refresh("owner", "accepted")
    value = await container.sync_repository.settings()
    config = container.sync.config(value)
    config.update(source_type="local_xml", time_utc="07:00")
    await container.sync.save(config, value["revision"], "operator")
    replay = await container.sync.refresh("owner", "accepted")
    assert replay["id"] == accepted["id"]
    assert len(await container.jobs.pending()) == 1


def test_loopback_port_preflight_only_accepts_own_gateway(monkeypatch):
    """Занятый loopback порт нельзя принять за SSH-вход чужого или публичного сервиса."""
    import json
    import socket
    from types import SimpleNamespace

    from src.cli import check_project_port

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def docker(*args):
            """Разрешает только read-only Docker discovery собственного gateway."""
            if args[0] == "ps":
                return SimpleNamespace(stdout="own-id")
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "NetworkSettings": {
                                "Ports": {
                                    "8000/tcp": [{"HostPort": str(port), "HostIp": "127.0.0.1"}]
                                }
                            }
                        }
                    ]
                )
            )

        monkeypatch.setattr("src.cli.docker", docker)
        check_project_port("127.0.0.1", port, "operator-gateway")

        def exposed(*args):
            """Имитирует ошибочную публикацию своего gateway также на публичном IPv6."""
            result = docker(*args)
            if args[0] == "inspect":
                info = json.loads(result.stdout)
                info[0]["NetworkSettings"]["Ports"]["8000/tcp"].append(
                    {"HostPort": str(port), "HostIp": "::"}
                )
                result.stdout = json.dumps(info)
            return result

        monkeypatch.setattr("src.cli.docker", exposed)
        with pytest.raises(MapperError) as error:
            check_project_port("127.0.0.1", port, "operator-gateway")
        assert error.value.code == "gateway_public_binding"
        monkeypatch.setattr("src.cli.docker", lambda *args: SimpleNamespace(stdout=""))
        with pytest.raises(MapperError) as error:
            check_project_port("127.0.0.1", port, "operator-gateway")
        assert error.value.code == "port_busy"


@pytest.mark.parametrize(
    "version,valid", [("27.5.1", False), ("28.0.0", True), ("29.8.1", True), ("unknown", False)]
)
def test_gateway_requires_docker_with_fixed_loopback_publishing(version, valid):
    """Старый Docker не должен незаметно ослабить доверенный loopback-вход."""
    from src.cli import check_engine_version

    if valid:
        check_engine_version(version)
    else:
        with pytest.raises(MapperError) as error:
            check_engine_version(version)
        assert error.value.code == "docker_engine_version"
