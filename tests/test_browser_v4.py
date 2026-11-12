# tests/test_browser_v4.py
"""Настоящий Chromium с mock 1С/AI: новые поля, XSS, owner UI и события каталога."""

import asyncio
import json
import socket
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_catalog_events import onec_system as onec_system
from test_specification_v4 import extended_source as extended_source

from src.main import create_app

pytestmark = pytest.mark.e2e


async def test_v4_source_fields_and_operator_event_ui(onec_system, extended_source):
    """Отображает длинные новые реквизиты безопасно, сохраняет approve/null и права настроек."""
    container, *_ = onec_system

    async def ready():
        """Offline здоровье внешних систем явно подменено."""
        return {
            "ready": True,
            "dependencies": dict.fromkeys(
                ["database", "rabbitmq", "shared_embedding", "qdrant", "catalog"], True
            ),
        }

    container.ready = ready
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(container), access_log=False, log_level="error")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))

    async def worker():
        """Доставляет настоящий SQL outbox в processor вместо RabbitMQ."""
        while not server.should_exit:
            for job_id in await container.jobs.pending():
                await container.processor.process(job_id)
            await asyncio.sleep(0.05)

    processing = asyncio.create_task(worker())
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
            await page.goto(base + "/mapping")
            await page.locator("[data-import]").set_input_files(
                {
                    "name": "extended.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(extended_source, ensure_ascii=False).encode(),
                }
            )
            await expect(page.locator("[data-rows] tr")).to_have_count(3, timeout=15000)
            await expect(page.locator("[data-source-file]")).to_contain_text(
                extended_source["file_name"]
            )
            await expect(page.locator("[data-rows]")).to_contain_text("00000006836")
            await expect(page.locator("[data-rows]")).to_contain_text("ROSMA")
            assert await page.locator("[data-rows] img").count() == 0
            await page.get_by_role(
                "button", name="Выбрать соответствие для строки 1", exact=True
            ).click()
            await expect(page.locator("[data-source-meta]")).to_contain_text("Примечание:")
            await expect(page.locator("[data-source-meta]")).to_contain_text("00000006836")
            await page.keyboard.press("Escape")
            await page.locator("[data-filter]").fill("00000006836")
            await expect(page.locator("[data-rows] tr")).to_have_count(2)
            await page.locator("[data-approve]").click()
            await expect(page.locator("[data-approval-status]")).to_contain_text(
                "Результат утверждён"
            )
            await page.reload()
            await expect(page.locator("[data-source-file]")).to_contain_text(
                extended_source["file_name"]
            )
            await asyncio.to_thread(Path("test-results").mkdir, exist_ok=True)
            await page.screenshot(path="test-results/v4-source-fields.png", full_page=True)
            await page.goto(base + "/settings")
            await expect(page.locator('[name="expected_format"]')).to_be_disabled()
            await expect(page.locator("[data-event-count='received']")).to_have_text("0")
            await expect(page.locator("[data-event-journal]")).not_to_be_visible()
            assert "w" * 32 not in await page.content()
            await page.goto(base + "/operator")
            await page.locator("#operator-ticket").fill(await container.operator_access.issue())
            await page.locator("[data-operator-login]").click()
            await expect(page.locator("[data-operator-status]")).to_contain_text("активна")
            await page.goto(base + "/settings")
            await page.locator("#onec-connection summary").click()
            await expect(page.locator('[name="expected_format"]')).to_be_enabled()
            await expect(page.locator('[name="expected_format"]')).to_have_value("json")
            await expect(page.locator("[data-event-journal]")).to_be_visible()
            await expect(page.locator("[data-webhook-url]")).to_have_value(
                container.settings.app_public_base_url + "/api/v1/catalog/events"
            )
            await page.screenshot(path="test-results/v4-webhook-settings.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            await browser.close()
    finally:
        server.should_exit = True
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        await serving
