# tests/test_browser_v4.py
"""Настоящий Chromium с mock 1С/AI: новые поля, XSS, owner UI и события каталога."""

import asyncio
import json
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_catalog_events import onec_system as onec_system
from test_specification_v4 import extended_source as extended_source

from src.main import create_app

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("startup", ["normal", "slow_summary", "slow_session", "failed_session"])
async def test_v4_source_fields_and_operator_event_ui(onec_system, extended_source, startup):
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
    intercepted, release = asyncio.Event(), asyncio.Event()
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
            responses = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "response",
                lambda response: responses.append((response.status, urlsplit(response.url).path)),
            )

            async def hold_request(route):
                """Удерживает настоящий запрос до проверки UI, не подменяя успешный ответ."""
                intercepted.set()
                await release.wait()
                await route.continue_()

            async def reject_session(route):
                """Явно моделирует недоступную сессию без обращения к внешним системам."""
                await route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": {"message": "Подготовка сессии недоступна"}}),
                )

            if startup == "slow_summary":
                await page.route("**/api/v1/dashboard/summary", hold_request)
            elif startup == "slow_session":
                await page.route("**/api/v1/operator/session", hold_request)
            elif startup == "failed_session":
                await page.route("**/api/v1/operator/session", reject_session)
            await page.goto(base + "/mapping")
            upload = page.locator("[data-import]")
            submit = page.locator("[data-submit] button")
            if startup == "slow_session":
                await asyncio.wait_for(intercepted.wait(), timeout=15)
                await expect(upload).to_be_disabled()
                await expect(submit).to_be_disabled()
                await expect(page.locator("#source-id")).to_be_disabled()
                release.set()
            elif startup == "failed_session":
                await expect(page.locator("[data-notice]")).to_have_text(
                    "Подготовка сессии недоступна"
                )
                await expect(upload).to_be_disabled()
                await expect(submit).to_be_disabled()
                assert not errors
                await browser.close()
                return
            # Готовность формы независима от фоновой сводки и проверяется по самому input.
            try:
                await expect(upload).to_be_enabled(timeout=15000)
            except AssertionError as error:
                notice = await page.locator("[data-notice]").text_content()
                raise AssertionError(
                    f"Форма не готова: {notice}; JS: {errors}; HTTP: {responses}"
                ) from error
            await expect(submit).to_be_enabled()
            if startup == "slow_summary":
                await asyncio.wait_for(intercepted.wait(), timeout=15)
                await expect(page.locator("[data-catalog-date]")).to_have_text("Проверка каталога…")
            await page.locator("[data-import]").set_input_files(
                {
                    "name": "extended.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(extended_source, ensure_ascii=False).encode(),
                }
            )
            await expect(page.locator("[data-rows] tr")).to_have_count(3, timeout=15000)
            release.set()
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
        release.set()
        server.should_exit = True
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        await serving
