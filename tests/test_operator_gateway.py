# tests/test_operator_gateway.py
"""Доказанный SSH-вход, proxy boundaries и выдача кода без публикации административного секрета."""

import asyncio
import socket
from pathlib import Path
from time import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from itsdangerous.timed import TimestampSigner
from playwright.async_api import async_playwright, expect

from src.main import create_app
from src.operator_gateway import create_gateway
from src.presentation.gateway_security import (
    GATEWAY_HEADER,
    has_gateway_proof,
    issue_gateway_proof,
)
from src.presentation.security import COOKIE, serializer


def proof_request(
    settings,
    *,
    proof=None,
    cookie=None,
    method="GET",
    path="/api/v1/operator/session",
    origin="http://localhost:18087",
    host="localhost:18087",
):
    """Создаёт delivery Request для проверки подписи без сокетов и подмены проверки прав."""
    app = FastAPI()
    app.state.container = SimpleNamespace(settings=settings)
    cookie = cookie or serializer(settings).dumps("test-session")
    if proof is None:
        proof = issue_gateway_proof(settings, method, path, cookie, origin, host)
    headers = {
        "host": host,
        "cookie": COOKIE + "=" + cookie,
        "origin": origin,
        GATEWAY_HEADER.lower(): proof,
    }
    return Request(
        {
            "type": "http",
            "app": app,
            "scheme": "http",
            "method": method,
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 1234),
        }
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"cookie": "bad-cookie"},
        {"host": "other.test"},
        {"origin": "http://evil.test"},
        {"method": "POST"},
        {"path": "/api/v1/operator/tickets"},
        {"proof": "forged"},
    ],
)
def test_gateway_proof_is_bound_to_request(settings, changed):
    """Cookie/Host/Origin/method/path нельзя подменить после подписи gateway."""
    cookie = serializer(settings).dumps("bound-session")
    proof = issue_gateway_proof(
        settings,
        "GET",
        "/api/v1/operator/session",
        cookie,
        "http://localhost:18087",
        "localhost:18087",
    )
    assert has_gateway_proof(proof_request(settings, proof=proof, cookie=cookie))
    args = {"proof": proof, "cookie": cookie, **changed}
    assert not has_gateway_proof(proof_request(settings, **args))


def test_gateway_proof_expires(settings, monkeypatch):
    """Перехваченный proof перестаёт действовать через 15 секунд."""
    cookie = serializer(settings).dumps("bound-session")
    with monkeypatch.context() as context:
        context.setattr(TimestampSigner, "get_timestamp", lambda self: int(time()) - 16)
        proof = issue_gateway_proof(
            settings,
            "GET",
            "/api/v1/operator/session",
            cookie,
            "http://localhost:18087",
            "localhost:18087",
        )
    assert not has_gateway_proof(proof_request(settings, cookie=cookie, proof=proof))


async def test_gateway_issue_login_csrf_and_public_host_spoof(system):
    """Только реальный proxy proof разрешает кнопку; обычный localhost Host не выдаёт код."""
    container, *_ = system
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(container)))
    gateway = create_gateway(container.settings, upstream)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:18087"
        ) as client:
            csrf = (await client.get("/api/v1/session")).json()["csrf"]
            session = await client.get("/api/v1/operator/session")
            assert session.status_code == 200 and session.json()["can_issue_ticket"]
            assert (await client.post("/api/v1/operator/tickets")).status_code == 403
            headers = {"Origin": "http://localhost:18087", "X-CSRF-Token": csrf}
            issued = await client.post("/api/v1/operator/tickets", headers=headers)
            assert issued.status_code == 201, issued.text
            assert len(issued.json()["ticket"]) >= 32
            assert issued.json()["expires_in"] == 120
            assert "b" * 32 not in issued.text and "c" * 32 not in issued.text
            login = await client.post(
                "/api/v1/operator/session",
                headers=headers,
                json={"ticket": issued.json()["ticket"]},
            )
            assert login.status_code == 200 and login.json()["operator"]
            assert (await client.get("/api/v1/operator/session")).json()["operator"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(container)),
            base_url="http://localhost:18087",
        ) as public:
            csrf = (await public.get("/api/v1/session")).json()["csrf"]
            assert not (await public.get("/api/v1/operator/session")).json()["can_issue_ticket"]
            denied = await public.post(
                "/api/v1/operator/tickets",
                headers={
                    "Origin": "http://localhost:18087",
                    "X-CSRF-Token": csrf,
                    GATEWAY_HEADER: "spoofed",
                    "Forwarded": "for=127.0.0.1;proto=https",
                },
            )
            assert denied.status_code == 403
    finally:
        await upstream.aclose()


class ResponseChunks(httpx.AsyncByteStream):
    """Сохраняет в mock transport настоящий ещё не прочитанный поток bytes."""

    async def __aiter__(self):
        """Передаёт две части тела для проверки проксирования без перекодировки."""
        yield b"first-"
        yield b"second"


async def test_gateway_fixed_origin_strip_headers_and_multiple_cookies(settings):
    """Пользователь не меняет upstream origin/доверенный header, Set-Cookie не схлопываются."""
    seen = []

    def handler(request):
        """Проверяет фактический запрос к ограниченному origin вместо сетевого вызова."""
        seen.append(request)
        assert request.url.host == "api"
        assert request.url.path == "//evil.test/path"
        assert not any(
            name in request.headers
            for name in (
                GATEWAY_HEADER,
                "forwarded",
                "x-forwarded-host",
                "x-real-ip",
                "x-remove-me",
            )
        )
        return httpx.Response(
            200,
            stream=ResponseChunks(),
            headers=[
                ("Set-Cookie", "one=1; HttpOnly"),
                ("Set-Cookie", "two=2; SameSite=Strict"),
                ("Content-Length", "12"),
                ("Connection", "close"),
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        gateway = create_gateway(settings, upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:18087"
        ) as client:
            response = await client.get(
                "http://localhost:18087//evil.test/path",
                headers={
                    GATEWAY_HEADER: "injected",
                    "Forwarded": "for=127.0.0.1",
                    "X-Real-IP": "127.0.0.1",
                    "X-Forwarded-Host": "localhost",
                    "Connection": "x-remove-me",
                    "X-Remove-Me": "bad",
                },
            )
            assert response.content == b"first-second"
            assert len(response.headers.get_list("set-cookie")) == 2
            assert "connection" not in response.headers
            assert len(seen) == 1


async def test_gateway_rejects_rebinding_host_and_masks_upstream_failure(settings):
    """Host чужого домена не получает доступ к loopback gateway; сетевой отказ безопасен."""

    def handler(request):
        """Подменяет недоступность внутреннего API, не создавая настоящего подключения."""
        raise httpx.ConnectError("private diagnostic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        gateway = create_gateway(settings, upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:18087"
        ) as client:
            denied = await client.get("/", headers={"Host": "evil.test"})
            assert denied.status_code == 403
            failure = await client.get("/health/ready")
            assert failure.status_code == 502
            assert "private diagnostic" not in failure.text


async def test_gateway_rejects_oversize_before_forward(settings):
    """Недоверенное тело ограничено до передачи внутреннему API."""
    settings.max_result_bytes = 1024

    def handler(request):
        """Не должен вызываться, поскольку oversized upload останавливает gateway."""
        pytest.fail("Oversized upload reached upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_gateway(settings, upstream)),
            base_url="http://localhost:18087",
        ) as client:
            response = await client.post("/api/v1/operator/tickets", content=b"x" * 2048)
            assert response.status_code == 413


@pytest.mark.e2e
async def test_browser_gateway_button_and_public_fallback(system):
    """Проверяет кнопку выдачи кода за gateway, ручной ввод и очистку после входа."""
    container, *_ = system

    async def ready():
        """Подменяет только внешние зависимости, честно оставляя SQL/auth/proxy настоящими."""
        return {
            "ready": True,
            "dependencies": dict.fromkeys(
                ("database", "rabbitmq", "shared_embedding", "qdrant", "catalog"), True
            ),
        }

    container.ready = ready
    sockets = []
    for _ in range(2):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sockets.append(sock)
    api_port, gateway_port = (sock.getsockname()[1] for sock in sockets)
    gateway_settings = container.settings.model_copy(
        update={"operator_gateway_base_url": f"http://127.0.0.1:{api_port}"}
    )
    servers = [
        uvicorn.Server(uvicorn.Config(create_app(container), log_level="error", access_log=False)),
        uvicorn.Server(
            uvicorn.Config(create_gateway(gateway_settings), log_level="error", access_log=False)
        ),
    ]
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]))
        for server, sock in zip(servers, sockets, strict=True)
    ]
    try:
        for _ in range(100):
            if all(server.started for server in servers):
                break
            await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"http://127.0.0.1:{gateway_port}/operator")
            await expect(page.locator("[data-get-ticket]")).to_be_enabled()
            await expect(page.locator("[data-ssh-instructions]")).to_be_hidden()
            await page.locator("[data-get-ticket]").click()
            await expect(page.locator("[data-issued-ticket-panel]")).to_be_visible()
            ticket = await page.locator("[data-issued-ticket]").input_value()
            assert len(ticket) >= 32
            await expect(page.locator("#operator-ticket")).to_have_value("")
            await page.locator("#operator-ticket").fill(ticket)
            await page.locator("[data-operator-login]").click()
            await expect(page.locator("[data-operator-status]")).to_contain_text("активна")
            await expect(page.locator("[data-issued-ticket]")).to_have_value("")
            await asyncio.to_thread(Path("test-results").mkdir, exist_ok=True)
            await page.screenshot(path="test-results/browser-operator-gateway.png", full_page=True)
            await page.locator("[data-operator-logout]").click()
            await expect(page.locator("[data-get-ticket]")).to_be_enabled()
            await page.goto(f"http://127.0.0.1:{api_port}/operator")
            await expect(page.locator("[data-ticket-issue]")).to_be_hidden()
            await expect(page.locator("[data-ssh-instructions]")).to_be_visible()
            await page.locator("[data-ssh-instructions]").evaluate("node => node.open = true")
            await page.locator("[data-ssh-user]").fill("alice")
            ssh_host = urlsplit(container.settings.app_public_base_url).hostname
            await expect(page.locator("[data-ssh-command]")).to_have_text(
                f"ssh -N -L 18087:127.0.0.1:{container.settings.operator_port} alice@{ssh_host}"
            )
            assert "main@" not in await page.content()
            assert not errors
            await browser.close()
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
