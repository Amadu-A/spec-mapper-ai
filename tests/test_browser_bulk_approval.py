# tests/test_browser_bulk_approval.py
"""Chromium: общее утверждение, крестик/null, revision и сеть; внешние AI/broker явно fake."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from openpyxl import load_workbook
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect

from src.main import create_app

pytestmark = pytest.mark.e2e


@asynccontextmanager
async def export_read_proxy(base):
    """Инъецирует разрыв TCP перед ответом GET, затем передаёт настоящий HTTP с теми же cookie."""
    endpoint = urlsplit(base)
    control = {"attempts": 0, "resets": 1, "http_status": None}

    async def accept(reader, writer):
        """Проверяет только чтения: transient reset, постоянный reset и HTTP 503 различаются."""
        upstream = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            assert header.startswith(b"GET ")
            control["attempts"] += 1
            if control["resets"]:
                control["resets"] -= 1
                writer.transport.abort()
                return
            if control["http_status"]:
                writer.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            source, upstream = await asyncio.open_connection(endpoint.hostname, endpoint.port)
            lines = [
                line
                for line in header.split(b"\r\n")
                if line and not line.lower().startswith(b"connection:")
            ]
            upstream.write(b"\r\n".join(lines) + b"\r\nConnection: close\r\n\r\n")
            await upstream.drain()
            while chunk := await source.read(65536):
                writer.write(chunk)
                await writer.drain()
        finally:
            for stream in [upstream, writer]:
                if stream is not None:
                    stream.close()
                    with suppress(ConnectionError):
                        await stream.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", control
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("reset_read", [False, True])
async def test_bulk_approval_keeps_current_choices_and_clear_is_explicit_null(
    system, source, tmp_path, reset_read
):
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
            network = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "response",
                lambda response: network.append((response.status, urlsplit(response.url).path)),
            )
            page.on(
                "requestfailed",
                lambda request: network.append((request.failure, urlsplit(request.url).path)),
            )
            await page.goto(base + "/mapping")
            await expect(page.locator("[data-import]")).to_be_enabled(timeout=15000)
            await expect(page.locator('link[rel="icon"]')).to_have_attribute(
                "href", "/static/favicon.svg"
            )
            icon = await page.request.get(base + "/static/favicon.svg", max_retries=2)
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
            excel_link = page.locator("[data-export='result.xlsx']")
            await expect(excel_link).to_have_attribute("aria-disabled", "true")
            assert await excel_link.get_attribute("href") is None
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
            if reset_read:
                restored_id = page.url.split("job=", 1)[1].split("&", 1)[0]

                async def disconnect_status(route):
                    """Проверяет восстановление polling после одного отказа status GET."""
                    await route.abort("failed")

                await page.route(f"**/api/v1/mappings/{restored_id}", disconnect_status, times=1)
            reload_start = len(network)
            await page.reload()
            try:
                await expect(page.locator("[data-approval-status]")).to_contain_text(
                    "Результат утверждён", timeout=15000
                )
            except AssertionError as error:
                notice = await page.locator("[data-notice]").text_content()
                raise AssertionError(
                    f"Reload не завершён: {notice}; JS: {errors}; HTTP: {network[reload_start:]}"
                ) from error
            job_id = page.url.split("job=", 1)[1].split("&", 1)[0]
            if reset_read:
                async with export_read_proxy(base) as (proxy, control):
                    read_url = f"{proxy}/api/v1/mappings/{job_id}/source.json"
                    recovered = await page.request.get(read_url, max_retries=2)
                    assert recovered.status == 200 and await recovered.json() == document
                    assert control["attempts"] == 2
                    control.update(attempts=0, resets=0, http_status=503)
                    rejected = await page.request.get(read_url, max_retries=2)
                    assert rejected.status == 503 and control["attempts"] == 1
                    control.update(attempts=0, resets=3, http_status=None)
                    with pytest.raises(
                        PlaywrightError, match="ECONNRESET|socket hang up|Failed after"
                    ):
                        await page.request.get(read_url, max_retries=2)
                    assert control["attempts"] == 3
            # Повторяется только сетевой ECONNRESET на безопасном GET, не HTTP-ошибки/записи.
            source_response = await page.request.get(
                f"{base}/api/v1/mappings/{job_id}/source.json", max_retries=2
            )
            assert source_response.status == 200
            assert await source_response.json() == document
            approved = await page.request.get(
                f"{base}/api/v1/mappings/{job_id}/approved.json", max_retries=2
            )
            assert approved.status == 200
            snapshot = await approved.json()
            assert [item["onec"] is None for item in snapshot["items"]] == [True, False, True]
            assert all(item["matching"]["confirmed"] for item in snapshot["items"])
            await expect(excel_link).to_have_attribute(
                "href", f"/api/v1/mappings/{job_id}/result.xlsx"
            )
            async with page.expect_download() as downloaded:
                await excel_link.click()
            excel_path = tmp_path / "mapping.xlsx"
            await (await downloaded.value).save_as(excel_path)
            sheet = load_workbook(excel_path).active
            assert sheet.title == "Лист_1" and sheet.max_column == 25 and sheet.max_row == 4
            assert sheet["T2"].value is None and sheet["T3"].value
            assert [sheet[f"Y{index}"].value for index in range(2, 5)] == [0, 1, 2]
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
