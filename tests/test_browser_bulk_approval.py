# tests/test_browser_bulk_approval.py
"""Chromium: общее утверждение, крестик/null, revision и сеть; внешние AI/broker явно fake."""

import asyncio
import json
import socket
from copy import deepcopy
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from src.main import create_app

pytestmark = pytest.mark.e2e


async def test_bulk_approval_keeps_current_choices_and_clear_is_explicit_null(system, source):
    """Не требует покликового ревью, не выбирает Top-1 вместо null, сохраняет source дословно."""
    container, *_ = system
    container.settings.match_threshold = 1.0
    document = deepcopy(source)
    document["items"].append({**document["items"][0], "Units": "м"})

    async def ready():
        """Подменяет только здоровье внешних служб в явно offline браузерном сценарии."""
        return {
            "ready": True,
            "dependencies": dict.fromkeys(
                ("database", "rabbitmq", "shared_embedding", "qdrant", "catalog"), True
            ),
        }

    container.ready = ready
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(container), log_level="error", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))

    async def worker():
        """Доставляет outbox в реальный processor/SQLite, заменяя только RabbitMQ транспорт."""
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
            context = await browser.new_context(viewport={"width": 1560, "height": 1080})
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/mapping")
            await expect(page.locator("[data-import]")).to_be_enabled(timeout=15000)
            await expect(page.locator('link[rel="icon"]')).to_have_attribute(
                "href", "/static/favicon.svg"
            )
            icon = await page.request.get(base + "/static/favicon.svg")
            assert icon.status == 200 and "image/svg+xml" in icon.headers["content-type"]
            await page.locator("[data-import]").set_input_files(
                {
                    "name": "bulk.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(document, ensure_ascii=False).encode(),
                }
            )
            await expect(page.locator("[data-rows] tr")).to_have_count(3)
            await expect(page.locator("[data-approve]")).to_be_enabled()
            await expect(page.locator("[data-approval-status]")).to_contain_text(
                "сразу во всей спецификации"
            )
            rows = page.locator("[data-rows] tr")
            await expect(rows.nth(0)).to_contain_text("Проверить")
            await expect(rows.nth(2)).to_contain_text("Соответствие не выбрано")
            assert await rows.nth(2).locator("[data-clear-selection]").count() == 0

            async def disconnect(route):
                """Обрывает DELETE до доставки, чтобы UI сохранил прежний выбранный элемент."""
                await route.abort("failed")

            await page.route("**/rows/0/selection?*", disconnect)
            await page.get_by_role("button", name="Очистить соответствие для строки 1").click()
            await expect(page.locator("[data-notice]")).to_contain_text("Нет соединения")
            await expect(rows.nth(0)).not_to_contain_text("Соответствие не выбрано")
            await expect(
                page.get_by_role("button", name="Очистить соответствие для строки 1")
            ).to_be_enabled()
            await page.unroute("**/rows/0/selection?*", disconnect)

            other = await context.new_page()
            await other.goto(page.url)
            await other.get_by_role("button", name="Очистить соответствие для строки 1").click()
            await expect(other.locator("[data-rows] tr").nth(0)).to_contain_text(
                "Подтверждено человеком"
            )
            # Первая вкладка ещё имеет revision до очистки в другой вкладке.
            second_clear = page.get_by_role("button", name="Очистить соответствие для строки 2")
            await second_clear.focus()
            await page.keyboard.press("Enter")
            await expect(page.locator("[data-notice]")).to_contain_text("другой вкладке")
            await expect(rows.nth(0)).to_contain_text("Соответствие не выбрано")
            await expect(rows.nth(0)).to_contain_text("Не найдено")
            await expect(rows.nth(0)).to_contain_text("Подтверждено человеком")
            await expect(rows.nth(1)).not_to_contain_text("Соответствие не выбрано")
            await expect(page.locator("[data-approve]")).to_be_enabled()

            await page.locator("[data-approve]").click()
            await expect(page.locator("[data-approval-status]")).to_contain_text(
                "Результат утверждён"
            )
            await expect(rows.nth(1)).to_contain_text("Подтверждено человеком")
            await expect(rows.nth(2)).to_contain_text("Не найдено")
            await expect(rows.nth(2)).to_contain_text("Подтверждено человеком")
            await expect(second_clear).to_be_disabled()
            assert not await page.locator("[data-drawer]").is_visible()
            for node in await page.locator("[data-select-row]").all():
                await expect(node).to_be_disabled()
            await page.reload()
            await expect(page.locator("[data-approval-status]")).to_contain_text(
                "Результат утверждён"
            )
            job_id = page.url.split("job=", 1)[1].split("&", 1)[0]
            source_response = await page.request.get(f"{base}/api/v1/mappings/{job_id}/source.json")
            assert await source_response.json() == document
            approved = await page.request.get(f"{base}/api/v1/mappings/{job_id}/approved.json")
            snapshot = await approved.json()
            assert [item["onec"] is None for item in snapshot["items"]] == [True, False, True]
            assert all(item["matching"]["confirmed"] for item in snapshot["items"])
            for original, result in zip(document["items"], snapshot["items"], strict=True):
                assert all(result[key] == value for key, value in original.items())
            await asyncio.to_thread(Path("test-results").mkdir, exist_ok=True)
            await page.screenshot(path="test-results/browser-bulk-approval.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await page.screenshot(
                path="test-results/browser-bulk-approval-mobile.png", full_page=True
            )
            icon_page = await context.new_page()
            await icon_page.goto(base + "/static/favicon.svg")
            await expect(icon_page.locator("svg")).to_be_visible()
            await icon_page.locator("svg").screenshot(path="test-results/browser-favicon.png")
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await server_task
