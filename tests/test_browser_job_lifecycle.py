# tests/test_browser_job_lifecycle.py
"""Chromium/API/SQL lifecycle E2E; внешние AI/AMQP порты явно подменены fixture."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import text
from test_browser_invoices import browser_server, stop_server
from test_invoices import invoice_source as invoice_source
from test_invoices import invoice_system as invoice_system
from test_invoices import pdf_bytes

pytestmark = pytest.mark.e2e


async def test_lost_post_ten_clicks_retry_navigation_and_new_job(invoice_system, invoice_source):
    """Потерянный 202 не вращает key; F5/навигация не POST, новая форма не отменяет старую."""
    container, _ = invoice_system
    async with container.engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
    gate = asyncio.Event()
    original = container.processor.process

    async def blocked(*args, **kwargs):
        """Удерживает offline worker до явного разрешения тестом."""
        await gate.wait()
        return await original(*args, **kwargs)

    container.processor.process = blocked
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1366, "height": 768})
            page = await context.new_page()
            errors, keys = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def lost_response(route):
                """Первый POST реально committed; теряется только HTTP ответ в browser transport."""
                keys.append(route.request.headers["idempotency-key"])
                response = await route.fetch()
                assert response.status == 202
                if len(keys) == 1:
                    await route.abort("connectionreset")
                else:
                    await route.fulfill(response=response)

            await page.route("**/api/v1/invoices/import-json", lost_response)
            await page.goto(base + "/invoices")
            field = page.locator("[data-invoice-import]")
            await expect(field).to_be_enabled()
            await field.set_input_files(
                {
                    "name": "synthetic.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(invoice_source).encode(),
                }
            )
            await page.evaluate("""() => {
                const field=document.querySelector('[data-invoice-import]');
                for(let i=0;i<10;i++)field.dispatchEvent(new Event('change'));
            }""")
            await expect(page.locator("[data-job-retry-submit]")).to_be_visible()
            assert len(keys) == 1
            await page.locator("[data-job-retry-submit]").click()
            await expect(page.locator("[data-lifecycle-stage]")).to_have_text("В очереди…")
            assert len(keys) == 2 and keys[0] == keys[1]
            first = page.url.split("job=")[1]
            await expect(field).to_be_disabled()
            await page.goto(base + "/mapping")
            await page.goto(base + "/invoices")
            await expect(page).to_have_url(base + "/invoices?job=" + first)
            await page.reload()
            await expect(page.locator("[data-lifecycle-stage]")).to_have_text("В очереди…")
            assert len(keys) == 2
            assert await page.evaluate("Object.keys(localStorage)") == []
            await page.locator("[data-job-new]").click()
            await expect(field).to_be_enabled()
            changed = deepcopy(invoice_source)
            changed["files"][0]["source_file"] = "second.pdf"
            await field.set_input_files(
                {
                    "name": "second.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(changed).encode(),
                }
            )
            await expect(page.locator("[data-job-active-count]")).to_have_text("Активных: 2")
            assert len(keys) == 3 and keys[2] != keys[1]
            assert page.url.split("job=")[1] != first
            await page.locator("[data-job-history]").select_option(first)
            await expect(page).to_have_url(base + "/invoices?job=" + first)
            assert len(keys) == 3 and not errors
            await browser.close()
    finally:
        gate.set()
        await stop_server(server, serving, processing)


async def test_compact_three_files_queued_cancel_and_mobile(invoice_system):
    """Показывает реальные выбранные страницы, неизвестные totals и отмену без запуска VLM."""
    container, _ = invoice_system
    gate, calls = asyncio.Event(), []

    async def blocked(identifier, *args):
        """Останавливает offline handler до claim: queued cancel не доходит до AI адаптеров."""
        await gate.wait()
        calls.append(identifier)

    container.processor.process = blocked
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1366, "height": 768})
            await page.goto(base + "/invoices")
            files = page.locator("[data-invoice-files]")
            await expect(files).to_be_enabled()
            await files.set_input_files(
                [
                    {"name": f"safe-{i}.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(4)}
                    for i in range(3)
                ]
            )
            for i in range(3):
                await page.locator(f"[data-invoice-file-pages='{i}']").fill("1-2,4")
            await page.evaluate("""() => {
                const form=document.querySelector('[data-invoice-upload]');
                for(let i=0;i<10;i++)form.dispatchEvent(new Event('submit',{cancelable:true}));
            }""")
            await expect(page.locator("[data-lifecycle-stage]")).to_have_text("В очереди…")
            await expect(page.locator("[data-lifecycle-files] .job-lifecycle__file")).to_have_count(
                3
            )
            await expect(page.locator("[data-lifecycle-package]")).to_contain_text(
                "Выбрано страниц: 9"
            )
            await expect(page.locator("[data-invoice-count='document_count']")).to_have_text("—")
            await expect(page.locator("[data-invoice-count='total']")).to_have_text("—")
            cancel = page.locator("[data-job-cancel]")
            box = await cancel.bounding_box()
            assert box and box["y"] + box["height"] < 768
            folder = Path(".cache/stage03b/screenshots")
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            await page.screenshot(path=str(folder / "invoice-queued-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(folder / "invoice-queued-mobile.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            await cancel.click()
            await expect(page.locator("[data-job-cancel-dialog]")).to_be_visible()
            await page.locator("[data-job-cancel-confirm]").click()
            await expect(page.locator("[data-lifecycle-stage]")).to_have_text("Отменено")
            await expect(cancel).to_be_hidden()
            assert not calls
            await browser.close()
    finally:
        await stop_server(server, serving, processing)


async def test_late_accepted_post_cannot_replace_explicit_new_workspace(
    invoice_system, invoice_source
):
    """Поздний ответ старого POST остаётся в history, но не переключает новый открытый job."""
    container, _ = invoice_system
    entered, release, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def waiting(*args):
        """Не позволяет offline worker завершить jobs во время гонки HTTP ответов."""
        await blocked.wait()

    container.processor.process = waiting
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            identifiers = []

            async def delayed(route):
                """Задерживает только первый уже принятый HTTP ответ."""
                response = await route.fetch()
                identifiers.append((await response.json())["job_id"])
                if len(identifiers) == 1:
                    entered.set()
                    await release.wait()
                await route.fulfill(response=response)

            await page.route("**/api/v1/invoices/import-json", delayed)
            await page.goto(base + "/invoices")
            field = page.locator("[data-invoice-import]")
            await expect(field).to_be_enabled()
            await field.set_input_files(
                {
                    "name": "first.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(invoice_source).encode(),
                }
            )
            await asyncio.wait_for(entered.wait(), 10)
            await page.locator("[data-job-new]").click()
            second = deepcopy(invoice_source)
            second["files"][0]["source_file"] = "second.pdf"
            await field.set_input_files(
                {
                    "name": "second.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(second).encode(),
                }
            )
            await expect(page.locator("[data-job-active-count]")).to_have_text("Активных: 2")
            assert len(identifiers) == 2
            await expect(page).to_have_url(base + "/invoices?job=" + identifiers[1])
            release.set()
            await expect(page.locator("[data-invoice-job]")).to_contain_text(identifiers[1])
            assert page.url.endswith(identifiers[1])
            await browser.close()
    finally:
        release.set()
        await stop_server(server, serving, processing)


async def test_sse_eof_fallback_and_expiry_clear_private_dom(invoice_system, invoice_source):
    """Chromium EOF включает bounded fallback; 410 очищает открытый private dialog."""
    container, _ = invoice_system
    gate = asyncio.Event()
    original = container.processor.process

    async def blocked(*args):
        """Сохраняет queued до наблюдения EOF тестового SSE HTTP ответа."""
        await gate.wait()
        return await original(*args)

    container.processor.process = blocked
    base, server, serving, processing = await browser_server(container)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def eof(route):
                """Сетевой fixture закрывает HTTP SSE; внешние сервисы не затрагиваются."""
                await route.fulfill(
                    status=200, content_type="text/event-stream", body="retry: 3000\n\n"
                )

            await page.route("**/api/v1/invoices/*/events?*", eof)
            await page.goto(base + "/invoices")
            field = page.locator("[data-invoice-import]")
            await expect(field).to_be_enabled()
            await field.set_input_files(
                {
                    "name": "safe.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(invoice_source).encode(),
                }
            )
            await expect(page.locator("[data-lifecycle-connection]")).to_contain_text(
                "Соединение потеряно"
            )
            gate.set()
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(5, timeout=25000)
            await expect(page.locator("[data-lifecycle-connection]")).to_have_text("")
            await page.get_by_role(
                "button", name="Выбрать соответствие для строки 1", exact=True
            ).click()
            await expect(page.locator("[data-source-caption]")).not_to_be_empty()
            identifier = page.url.split("job=")[1]
            job = await container.jobs.get(identifier)
            container.jobs.clock = lambda: job["expires_at"] + 1
            await page.evaluate("window.dispatchEvent(new Event('focus'))")
            await expect(page.locator("[data-invoice-rows] tr")).to_have_count(0)
            await expect(page.locator("[data-drawer]")).not_to_be_visible()
            await expect(page.locator("[data-source-caption]")).to_be_empty()
            await expect(page.locator("[data-candidates]")).to_be_empty()
            await expect(page.locator("[data-lifecycle-files]")).to_be_empty()
            await expect(page.locator("[data-invoice-job]")).to_be_empty()
            await expect(page.locator("[data-invoice-count='total']")).to_have_text("—")
            await expect(page.locator("[data-invoice-export='result.xlsx']")).not_to_have_attribute(
                "href"
            )
            assert await field.input_value() == ""
            await browser.close()
    finally:
        gate.set()
        await stop_server(server, serving, processing)
