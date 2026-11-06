# src/infrastructure/integration_status.py
"""Ограниченные проверки upstream без чужих документов; Telegram здесь входящий API-клиент."""

from __future__ import annotations

import asyncio
from time import monotonic, time

import httpx

from src.domain.models import MapperError
from src.infrastructure.http_clients import bounded_json


class IntegrationProbes:
    """Кэширует безопасную проверку extracting; HTTP 1С никогда не опрашивается фоновым GET UI."""

    def __init__(self, client, settings):
        """Использует общий bounded client и server key, который не возвращается вызывающему."""
        self.client, self.settings = client, settings
        self.cached, self.checked, self.lock = {}, 0.0, asyncio.Lock()

    async def extracting(self) -> dict:
        """404 защищённого маршрута по несуществующему UUID проверяет ключ без доступа к
        документам.
        """
        async with self.lock:
            if self.cached and monotonic() - self.checked < 15:
                return dict(self.cached)
            result = {
                "state": "unavailable",
                "available": False,
                "checked_at": time(),
                "key_configured": bool(self.settings.extracting_api_key.get_secret_value()),
            }
            try:
                async with asyncio.timeout(8):
                    root = self.settings.extracting_base_url.rstrip("/")
                    schema = await bounded_json(
                        self.client, "GET", root + "/openapi.json", 2000000, timeout=5
                    )
                    if "/api/v1/extractions/{job_id}" not in schema.get("paths", {}):
                        result["error_code"] = "extracting_contract_unavailable"
                    elif not result["key_configured"]:
                        result.update(state="key_missing", available=True)
                    else:
                        response = await self.client.get(
                            root + "/api/v1/extractions/00000000-0000-4000-8000-000000000000",
                            headers={
                                "X-API-Key": self.settings.extracting_api_key.get_secret_value()
                            },
                            timeout=5,
                        )
                        if response.status_code in {404, 410}:
                            result.update(state="available", available=True, authorized=True)
                        elif response.status_code in {401, 403}:
                            result.update(state="unauthorized", available=True, authorized=False)
                        else:
                            result["error_code"] = "extracting_probe_rejected"
            except (httpx.HTTPError, TimeoutError, ValueError, MapperError):
                # Предметные bounded_json ошибки не должны раскрыть URL или body внешнего API.
                result["error_code"] = "extracting_unavailable"
            self.cached, self.checked = result, monotonic()
            return dict(result)
