# src/infrastructure/invoices/extracting.py
"""Серверный invoice API 8083: общий bounded transport/polling, приватные ID и preview."""

import json

import httpx

from src.domain.invoices.models import validate_invoice
from src.domain.models import MapperError
from src.infrastructure.http_clients import HttpExtractionSource


class HttpInvoiceExtraction(HttpExtractionSource):
    """Переиспользует транспорт спецификаций, но никогда не их source-валидатор."""

    profile_path = "/api/v1/invoice-extractions"

    async def submit(self, metadata: dict, files: list[bytes], key: str) -> str:
        """Передаёт тот же пакет и стабильный key после restart/потерянного ответа."""
        return await self._submit(
            "/api/v1/invoice-extractions",
            headers={"Idempotency-Key": key},
            files=[
                ("files", (item["filename"], content, item["media_type"]))
                for item, content in zip(metadata["files"], files, strict=True)
            ],
            data={"page_selection": json.dumps(metadata["page_selection"])},
        )

    async def fetch(self, source_job_id: str, progress) -> dict:
        """Скачивает полный invoice JSON, отмечая completed_with_warnings отдельно."""

        async def completed(state: str) -> None:
            """Передаёт только безопасный финальный статус в durable checkpoint Mapper."""
            await progress("fetching", upstream_status=state)

        return await self.fetch_profile(
            source_job_id,
            progress,
            "/api/v1/invoice-extractions",
            lambda payload: validate_invoice(payload, self.settings.invoice_max_items),
            wait_seconds=self.settings.invoice_upstream_timeout_seconds,
            on_complete=completed,
        )

    async def preview(self, job_id: str, file_index: int, page: int, width: int) -> bytes:
        """Проксирует ограниченный PNG с фиксированного origin; redirect/HTML запрещены."""
        key = self.settings.extracting_api_key.get_secret_value()
        if not key:
            raise MapperError("upstream_key_missing", "Не задан серверный ключ извлечения", 503)
        url = (
            self.settings.extracting_base_url.rstrip("/")
            + f"/api/v1/invoice-extractions/{job_id}/files/{file_index}/pages/{page}/preview"
        )
        try:
            async with self.client.stream(
                "GET", url, params={"width": width}, headers={"X-API-Key": key}
            ) as response:
                response.raise_for_status()
                if response.headers.get("Content-Type", "").split(";")[0] != "image/png":
                    raise MapperError(
                        "upstream_response", "Некорректное изображение источника", 502
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self.settings.max_result_bytes:
                        raise MapperError("response_too_large", "Слишком большое изображение", 502)
                    content.extend(chunk)
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise MapperError(
                        "upstream_response", "Некорректное изображение источника", 502
                    )
                return bytes(content)
        except httpx.HTTPStatusError as exc:
            names = {404: "upstream_not_found", 410: "upstream_expired"}
            raise MapperError(
                names.get(exc.response.status_code, "upstream_http_error"),
                "Просмотр источника временно недоступен",
                502,
            ) from exc
        except httpx.RequestError as exc:
            raise MapperError(
                "upstream_timeout", "Просмотр источника временно недоступен", 504
            ) from exc
