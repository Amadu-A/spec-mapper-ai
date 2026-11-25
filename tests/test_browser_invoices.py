# tests/test_browser_invoices.py
"""Настоящий браузер/API/SQLite: invoice UI с явно подменёнными VLM/AI/broker зависимостями."""

import asyncio
import hashlib
import json
import socket
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_invoices import (
    image_bytes,
    pdf_bytes,
)
from test_invoices import (
    invoice_source as invoice_source,
)
from test_invoices import (
    invoice_system as invoice_system,
)

from src.infrastructure.invoices.extracting import HttpInvoiceExtraction
from src.main import create_app

pytestmark = pytest.mark.e2e


async def browser_server(container):
    """Поднимает временный HTTP API и SQL outbox worker; не выдаёт doubles за AMQP/VLM."""

    async def ready():
        """Offline готовность тестовых адаптеров, а не реальных серверных зависимостей."""
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
        """Исполняет общий processor из настоящего SQL outbox без настоящего RabbitMQ."""
        while not server.should_exit:
            for job_id in await container.jobs.pending():
                await container.processor.process(job_id)
            await asyncio.sleep(0.05)

    processing = asyncio.create_task(worker())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    return base, server, serving, processing


async def stop_server(server, serving, processing):
    """Закрывает только собственный loopback сервер и тестовый worker."""
    server.should_exit = True
    processing.cancel()
    await asyncio.gather(processing, return_exceptions=True)
    await serving


async def test_browser_invoice_review_reload_approval_exports(invoice_system, invoice_source):
    """Проверяет XSS, Top-5/поиск/null, reload, finance acknowledgment, экспорт, mobile и TTL."""
    container, catalog = invoice_system
    source = deepcopy(invoice_source)
    source["files"][0]["documents"][0]["items"][0]["caption"] += ' <img src=x onerror="alert(1)">'
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1680, "height": 1100})
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/invoices")
            await expect(page.locator("[data-invoice-import]")).to_be_enabled()
            await page.locator("[data-invoice-import]").set_input_files(
                {
                    "name": "invoice.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(source, ensure_ascii=False).encode(),
                }
            )
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(5)
            assert await page.locator("[data-invoice-rows] img").count() == 0
            await expect(page.locator("[data-invoice-rows]")).to_contain_text("00100.00")
            await expect(page.locator("[data-invoice-approve]")).to_be_disabled()
            await page.get_by_role(
                "button", name="Выбрать соответствие для строки 1", exact=True
            ).click()
            await expect(page.locator("[data-candidates] .candidate")).to_have_count(5)
            await page.locator("[data-search-candidates]").fill(catalog[-1].code)
            await page.locator("[data-search-results] input").first.check()
            await page.locator("[data-save-selection]").click()
            await expect(page.locator("[data-invoice-row='0']")).to_contain_text("Вручную")
            await page.get_by_role(
                "button", name="Очистить соответствие для строки 2", exact=True
            ).click()
            await expect(page.locator("[data-invoice-row='1']")).to_contain_text("Не найдено")
            await page.reload()
            await expect(page.locator("[data-invoice-row='0']")).to_contain_text("Вручную")
            await expect(page.locator("[data-invoice-row='1']")).to_contain_text(
                "Соответствие не выбрано"
            )
            await page.locator("[data-invoice-filter-status]").select_option("not_applicable")
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(1)
            await expect(
                page.get_by_role("button", name="Выбрать соответствие для строки 4", exact=True)
            ).to_be_disabled()
            await page.locator("[data-invoice-filter-status]").select_option("all")
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(5)
            await page.locator("[data-invoice-acknowledge='0:0']").check()
            await expect(page.locator("[data-invoice-approve]")).to_be_enabled()
            await page.locator("[data-invoice-approve]").click()
            await expect(page.locator("[data-invoice-approval]")).to_contain_text(
                "Результат утверждён"
            )
            await expect(page.locator("[data-invoice-export='result.xlsx']")).to_have_attribute(
                "aria-disabled", "false"
            )
            async with page.expect_download() as download:
                await page.locator("[data-invoice-export='result.xlsx']").click()
            assert (await download.value).suggested_filename == "invoice-mapping.xlsx"
            source_response = await context.request.get(
                base
                + (await page.locator("[data-invoice-export='source.json']").get_attribute("href"))
            )
            assert await source_response.json() == source
            await page.reload()
            await expect(page.locator("[data-invoice-approval]")).to_contain_text(
                "Результат утверждён"
            )
            await asyncio.to_thread(Path("test-results").mkdir, exist_ok=True)
            await page.evaluate("window.scrollTo(0,0)")
            await page.screenshot(path="test-results/browser-invoices.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.evaluate("window.scrollTo(0,0)")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path="test-results/browser-invoices-mobile.png", full_page=True)
            job_id = page.url.split("job=")[1]
            job = await container.jobs.get(job_id)
            container.jobs.clock = lambda: job["expires_at"] + 1
            await page.reload()
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(0)
            await expect(page.locator("[data-notice]")).to_contain_text("недоступно")
            assert not errors
            await browser.close()
    finally:
        await stop_server(server, serving, processing)


async def test_browser_local_swagger_without_cdn(invoice_system):
    """Настоящий Swagger UI загружается с локальными assets при CSP, без внешнего validator."""
    container, _ = invoice_system
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: errors.append(message.text) if message.type == "error" else None,
            )
            await page.goto(base + "/docs")
            await page.get_by_text("Интерактивный Swagger UI", exact=True).click()
            await (
                page.locator("#swagger-ui")
                .get_by_role("heading")
                .filter(has_text="Сопоставление счетов")
                .get_by_role("button")
                .click()
            )
            await expect(page.locator("#swagger-ui")).to_contain_text(
                "/api/v1/invoices/import-json"
            )
            assert not [
                url
                for url in requests
                if url.startswith(("http://", "https://")) and not url.startswith(base)
            ]
            assert not errors
            await browser.close()
    finally:
        await stop_server(server, serving, processing)


async def test_browser_invoice_multifile_and_preview(invoice_system, invoice_source):
    """Browser multipart → mock 8083 → настоящий worker/SQL → защищённый preview через 8087."""
    container, _ = invoice_system
    content, png = pdf_bytes(), image_bytes()
    upstream_id = str(uuid4())
    source = deepcopy(invoice_source)
    source["files"][0].update(source_sha256=hashlib.sha256(content).hexdigest())
    second = deepcopy(source["files"][0])
    second.update(
        file_index=1,
        source_file="two.png",
        source_sha256=hashlib.sha256(png).hexdigest(),
        page_count=1,
    )
    second["documents"][0]["attached_pages"] = []
    source["files"].append(second)
    source["summary"] = {"file_count": 2, "document_count": 2, "item_count": 10}

    async def upstream(request):
        """Контрактный double VLM; проверяет отдельный server-only ключ и конечный JSON."""
        assert request.headers["X-API-Key"] == "f" * 32
        if request.method == "POST":
            assert content in request.content and png in request.content
            return httpx.Response(202, json={"job_id": upstream_id})
        if request.url.path.endswith("preview"):
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        if request.url.path.endswith("result.json"):
            return httpx.Response(200, json=source)
        return httpx.Response(200, json={"status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        extraction = HttpInvoiceExtraction(client, container.settings)
        container.invoices.extraction = container.processor.invoice_extraction = extraction
        base, server, serving, processing = await browser_server(container)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page()
                await page.goto(base + "/invoices")
                await expect(page.locator("[data-invoice-files]")).to_be_enabled()
                await page.locator("[data-invoice-files]").set_input_files(
                    [
                        {"name": "one.pdf", "mimeType": "application/pdf", "buffer": content},
                        {"name": "two.png", "mimeType": "image/png", "buffer": png},
                    ]
                )
                await page.locator("[data-invoice-file-pages='0']").fill("1-2")
                await page.locator("[data-invoice-submit]").click()
                await expect(page.locator("[data-invoice-rows] tr")).to_have_count(10)
                await page.get_by_role("button", name="Исходный документ", exact=True).first.click()
                await expect(page.locator("[data-invoice-preview-image]")).to_be_visible()
                assert (
                    await page.locator("[data-invoice-preview-image]").evaluate(
                        "image => image.naturalWidth"
                    )
                    == 10
                )
                assert upstream_id not in await page.content()
                await page.keyboard.press("Escape")
                await expect(page.locator("[data-invoice-preview]")).not_to_be_visible()
                await browser.close()
        finally:
            await stop_server(server, serving, processing)
