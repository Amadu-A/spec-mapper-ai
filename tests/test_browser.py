# tests/test_browser.py
"""Браузерные E2E с настоящим FastAPI/SQLite и явно подменёнными внешними AI/broker портами."""

import asyncio
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from src.main import create_app

pytestmark = pytest.mark.e2e


async def test_browser_import_select_reload_and_export(system, source, tmp_path):
    """Проверяет upload, Top-K, ручной выбор, reload, фильтр и скачивание Excel в браузере."""
    import socket

    container, *_ = system

    async def ready():
        """Показывает тестовые зависимости честно как часть fake окружения браузерного теста."""
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
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(container), host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))

    async def worker():
        """Доставляет outbox в настоящий processor вместо RabbitMQ в браузерном test fixture."""
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
            page = await browser.new_page(viewport={"width": 1600, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{port}/")
            await expect(page.locator("[data-catalog-date]")).to_contain_text("Каталог:")
            import json

            await page.locator("[data-import]").set_input_files(
                {
                    "name": "specification.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(source, ensure_ascii=False).encode(),
                }
            )
            await page.locator("[data-rows] tr").first.wait_for(timeout=15000)
            assert await page.locator("[data-rows] tr").count() == 2
            await page.get_by_role(
                "button", name="Выбрать соответствие для строки 1", exact=True
            ).click()
            await page.locator("[data-candidates] input").first.check()
            await page.get_by_role("button", name="Сохранить выбор", exact=True).click()
            await expect(page.locator("[data-rows]")).to_contain_text("Вручную")
            await page.reload()
            await expect(page.locator("[data-rows]")).to_contain_text("Вручную")
            await page.locator("[data-status]").select_option("manual")
            await expect(page.locator("[data-rows] tr")).to_have_count(1)
            screenshot = Path("test-results/browser-mapping.png")
            screenshot.parent.mkdir(exist_ok=True)
            await page.screenshot(path=str(screenshot), full_page=True)
            async with page.expect_download() as downloaded:
                await page.locator("[data-export='result.xlsx']").click()
            download = await downloaded.value
            assert download.suggested_filename == "mapping.xlsx"
            await download.save_as(tmp_path / "mapping.xlsx")
            await page.get_by_role(
                "button", name="Выбрать соответствие для строки 1", exact=True
            ).click()
            await page.locator("[data-search-candidates]").fill("00-00001")
            await page.locator("[data-search-results] .candidate").first.wait_for()
            await page.screenshot(path="test-results/browser-selection.png", full_page=True)
            await page.keyboard.press("Escape")
            await expect(page.locator("[data-drawer]")).not_to_be_visible()
            assert not errors
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path="test-results/browser-mobile.png", full_page=True)
            await browser.close()
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await server_task
