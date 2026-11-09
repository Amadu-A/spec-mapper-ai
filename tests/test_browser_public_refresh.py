# tests/test_browser_public_refresh.py
"""Chromium: обычный пользователь обновляет подтверждённый mock HTTP источник с общим cooldown."""

import asyncio
import socket

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from src.main import create_app

pytestmark = pytest.mark.e2e


async def test_public_refresh_button_cooldown_reload_and_operator_only_fields(system, monkeypatch):
    """Настройки закрыты, кнопка работает без роли; другая сессия и reload не обходят минуту."""
    container, *_ = system
    clock = [1793966400.0]
    container.jobs.clock = lambda: clock[0]
    container.settings.onec_catalog_contract_confirmed = True
    container.settings.onec_catalog_allowed_hosts = ["onec.test"]
    current = await container.sync_repository.settings()
    config = container.sync.config(current)
    config.update(
        source_type="http_onec",
        url="https://onec.test/catalog",
        completion_header="X-Full",
        completion_value="yes",
    )
    await container.sync.save(config, current["revision"], "operator")
    current = await container.sync_repository.settings()
    await container.sync_repository.probe(
        current["revision"],
        {"state": "confirmed", "source_identity": container.catalog_sources.identity(current)},
    )
    monkeypatch.setattr(
        container.catalog_sources,
        "source",
        lambda config, progress: container.catalog_sources.local,
    )

    async def ready():
        """Внешние сервисы здесь явно fake; реальные адаптеры проверяются отдельной live suite."""
        return {
            "ready": True,
            "dependencies": dict.fromkeys(
                ["database", "rabbitmq", "shared_embedding", "qdrant", "catalog"], True
            ),
        }

    async def extracting():
        """Не выполняет внешний upstream probe во время браузерного offline сценария."""
        return {"state": "unavailable"}

    container.ready = ready
    container.integration_probes.extracting = extracting
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(container), log_level="error", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))

    async def worker():
        """Доставляет durable outbox в настоящий processor с явно fake broker transport."""
        while not server.should_exit:
            for job in await container.jobs.pending():
                await container.processor.process(job)
            await asyncio.sleep(0.05)

    worker_task = asyncio.create_task(worker())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            first = await browser.new_context(viewport={"width": 1440, "height": 1100})
            second = await browser.new_context()
            page = await first.new_page()
            other = await second.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/")
            await expect(page.locator("[data-sync-now]")).to_be_enabled()
            await expect(page.locator("[data-sync-time]")).to_be_disabled()
            await expect(page.locator("[data-sync-enabled]")).to_be_disabled()
            assert "onec.test" not in await page.content()
            await page.locator("[data-sync-now]").click()
            await expect(page.locator("[data-notice]")).to_contain_text("очередь")
            await expect(page.locator("[data-sync-now]")).to_be_disabled()
            await expect(page.locator("[data-sync-now]")).to_contain_text("через")
            await other.goto(base + "/")
            await expect(other.locator("[data-sync-now]")).to_be_disabled()
            await expect(other.locator("[data-refresh-status]")).to_contain_text("через")
            await page.reload()
            await expect(page.locator("[data-sync-now]")).to_be_disabled()
            await expect(page.locator("[data-sync-now]")).to_contain_text("через")
            for _ in range(100):
                if not await container.jobs.pending():
                    break
                await asyncio.sleep(0.05)
            clock[0] += 61
            await page.reload()
            await expect(page.locator("[data-sync-now]")).to_be_enabled()
            assert (await container.sync_repository.settings())["enabled"] is False
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await asyncio.wait_for(server_task, 10)
