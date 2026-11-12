# tests/test_browser_dashboard.py
"""Chromium E2E главной и операторских действий; внешние AI/broker явно подменены."""

import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from src.main import create_app

pytestmark = pytest.mark.e2e


async def test_dashboard_operator_schedule_import_history_navigation(system, source):
    """Проверяет все главные действия, reload/back, ошибки доступа и мобильную вёрстку."""
    container, *_ = system
    # Ускоренный E2E проходит много экранов за секунды; лимиты проверяются отдельно через API.
    container.settings.rate_limit_per_minute = 1000

    async def ready():
        """Явно mocked здоровье внешних сервисов для браузерного offline теста."""
        return {
            "ready": True,
            "dependencies": dict.fromkeys(
                ["database", "rabbitmq", "shared_embedding", "qdrant", "catalog"], True
            ),
        }

    async def extracting():
        """Явно mocked недоступность upstream без вымышленного подключения."""
        return {"state": "unavailable", "available": False}

    container.ready = ready
    container.integration_probes.extracting = extracting
    await container.sync_repository.touch("worker")
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
        """Доставляет реальные durable jobs без RabbitMQ, не выдавая это за live тест."""
        while not server.should_exit:
            for job_id in await container.jobs.pending():
                await container.processor.process(job_id)
            await asyncio.sleep(0.05)

    worker_task = asyncio.create_task(worker())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1680, "height": 1100})
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/")
            await expect(page.locator('[data-kpi="catalog"]')).not_to_have_text("—")
            await expect(page.locator("[data-sync-now]")).to_be_disabled()
            await expect(page.locator('[data-integration="onec"]')).to_have_text(
                "Ожидается контракт"
            )
            await page.locator("[data-operator-link]").click()
            await expect(page).to_have_url(base + "/operator")
            ticket = await container.operator_access.issue()
            await page.locator("#operator-ticket").fill(ticket)
            await page.locator("[data-operator-login]").click()
            await expect(page.locator("[data-operator-status]")).to_contain_text("активна")
            await expect(page.locator("#operator-ticket")).to_have_value("")
            await page.locator('.sidebar__link[href="/"]').click()
            await expect(page.locator("[data-sync-now]")).to_be_enabled()
            await page.locator("[data-sync-time]").fill("16:30")
            await page.locator("[data-sync-enabled]").check()
            await page.locator("[data-save-sync]").click()
            await expect(page.locator("[data-sync-save-status]")).to_contain_text("сохранены")
            await expect(page.locator("[data-sync-description]")).to_contain_text("16:30 UTC")
            await page.reload()
            await expect(page.locator("[data-sync-time]")).to_have_value("16:30")
            await expect(page.locator("[data-sync-enabled]")).to_be_checked()
            await page.locator("[data-sync-now]").click()
            await expect(page.locator("[data-notice]")).to_contain_text("очередь")
            await page.reload()
            await expect(page.locator("[data-sync-attempt]")).to_contain_text(
                "Завершено", timeout=15000
            )
            async with page.expect_download() as download:
                await page.locator("[data-sync-report]").click()
            assert (await download.value).suggested_filename == "catalog-import.json"
            await asyncio.to_thread(Path("test-results").mkdir, exist_ok=True)
            await page.screenshot(path="test-results/browser-dashboard.png", full_page=True)
            await page.locator('.sidebar__link[href="/events"]').click()
            await expect(page.locator("[data-history-items]")).to_contain_text("Завершено")
            await page.locator("[data-history-kind]").select_option("audit")
            await expect(page.locator("[data-history-items]")).to_contain_text("settings_saved")
            await page.go_back()
            await expect(page).to_have_url(base + "/")
            await page.locator('.sidebar__link[href="/settings"]').click()
            await expect(page.locator("[data-sync-time]")).to_have_value("16:30")
            await page.locator("[data-sync-enabled]").uncheck()
            await page.locator("[data-save-sync]").click()
            await expect(page.locator("[data-sync-next]")).to_contain_text("выключено")
            await page.locator("[data-sync-source]").select_option("http_onec")
            await page.locator("[data-save-sync]").click()
            await expect(page.locator("[data-sync-save-status]")).to_contain_text("сохранены")
            await page.locator("[data-test-source]").click()
            await expect(page.locator("[data-secret-state]")).to_contain_text("Ожидается контракт")
            await expect(page.locator("[data-sync-enabled]")).to_be_disabled()
            await expect(page.locator("[data-sync-now]")).to_be_disabled()
            await page.locator('.sidebar__link[href="/catalog"]').click()
            await page.locator("[data-catalog-query]").fill("00-00001")
            await expect(page.locator("[data-catalog-items]")).to_contain_text("00-00001")
            await page.locator('.sidebar__link[href="/"]').click()
            await page.set_viewport_size({"width": 390, "height": 844})
            await expect(page.locator('[data-kpi="catalog"]')).not_to_have_text("—")
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await page.screenshot(path="test-results/browser-dashboard-mobile.png", full_page=True)
            assert not errors
            await context.close()
            await browser.close()
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await asyncio.wait_for(server_task, 10)
