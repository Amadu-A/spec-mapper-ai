# src/infrastructure/http_clients.py
"""Ограниченные HTTP-клиенты upstream и embedding с безопасными предметными ошибками."""

from __future__ import annotations

import asyncio
import json
import math
from time import monotonic
from uuid import UUID

import httpx

from src.domain.models import MapperError, digest, validate_source


async def bounded_json(
    client: httpx.AsyncClient, method: str, url: str, max_bytes: int, **kwargs
) -> dict:
    """Ограничивает даже chunked/gzip-ответ по фактически прочитанному объёму."""
    try:
        async with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise MapperError(
                        "response_too_large", "Превышен размер ответа внешней системы"
                    )
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("Ожидался объект")
            return value
    except (json.JSONDecodeError, ValueError) as exc:
        raise MapperError("invalid_response", "Внешняя система вернула некорректный JSON") from exc


class HttpExtractionSource:
    """Опрос spec-extracting-ai по серверному API-ключу; чужие cookie не используются."""

    def __init__(self, client: httpx.AsyncClient, settings) -> None:
        """Принимает общий ограниченный HTTP pool и типизированные параметры upstream."""
        self.client, self.settings = client, settings

    async def _submit(self, path: str, **kwargs) -> str:
        """Проверяет bounded ответ upstream; credentials и тела ошибок не возвращает клиенту."""
        key = self.settings.extracting_api_key.get_secret_value()
        if not key:
            raise MapperError("upstream_key_missing", "Не задан серверный API-ключ извлечения", 503)
        headers = {"X-API-Key": key, **kwargs.pop("headers", {})}
        try:
            payload = await bounded_json(
                self.client,
                "POST",
                self.settings.extracting_base_url.rstrip("/") + path,
                1000000,
                headers=headers,
                **kwargs,
            )
            identifier = payload.get("job_id") or payload["id"]
            if not isinstance(identifier, str):
                raise ValueError("Expected UUID string")
            return str(UUID(identifier))
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            names = {
                400: "invalid_pdf",
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                409: "conflict",
                410: "expired",
                413: "upload_limit",
                422: "validation",
                429: "rate_limit",
            }
            raise MapperError(
                "upstream_" + names.get(code, "http_error"),
                "Сервис извлечения отклонил PDF или параметры",
                503 if code >= 500 or code == 429 else 422,
            ) from exc
        except httpx.RequestError as exc:
            raise MapperError("upstream_timeout", "Сервис извлечения недоступен", 503) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise MapperError(
                "upstream_response", "Некорректный ответ сервиса извлечения", 502
            ) from exc

    async def upload(self, content: bytes, filename: str) -> str:
        """Загружает PDF с серверным ключом; ID фиксируется worker до запуска анализа."""
        return await self._submit(
            "/api/v1/documents", files={"file": (filename, content, "application/pdf")}
        )

    async def start(self, document_id: str, pages: str, project_code: str, key: str) -> str:
        """Запускает извлечение идемпотентно по ID общего задания после сохранения документа."""
        return await self._submit(
            "/api/v1/extractions",
            headers={"Idempotency-Key": key},
            data={"document_id": document_id, "pages": pages, "project_code": project_code},
        )

    async def fetch(self, source_job_id: str, progress) -> dict:
        """Различает terminal ошибки, ждёт финал и ровно один раз скачивает result.json."""
        return await self.fetch_profile(
            source_job_id,
            progress,
            "/api/v1/extractions",
            lambda payload: validate_source(payload, self.settings.max_items),
        )

    async def fetch_profile(
        self,
        source_job_id: str,
        progress,
        path: str,
        validator,
        *,
        wait_seconds: int | None = None,
        on_complete=None,
    ) -> dict:
        """Общий bounded polling финального контракта, с отдельным валидатором профиля."""
        settings = self.settings
        if not settings.extracting_api_key.get_secret_value():
            raise MapperError("upstream_key_missing", "Не задан серверный API-ключ извлечения", 503)
        url = f"{settings.extracting_base_url.rstrip('/')}{path}/{source_job_id}"
        headers = {"X-API-Key": settings.extracting_api_key.get_secret_value()}
        wait_seconds = (
            wait_seconds if wait_seconds is not None else settings.upstream_timeout_seconds
        )
        deadline = monotonic() + wait_seconds

        async def get_json(path: str, limit: int) -> dict:
            """Ожидает снятия ограничения частоты в пределах общего срока ожидания извлечения."""
            while True:
                try:
                    return await bounded_json(self.client, "GET", path, limit, headers=headers)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 429:
                        raise
                    delay = max(3.0, settings.poll_seconds)
                    try:
                        requested = float(exc.response.headers.get("Retry-After", ""))
                        if math.isfinite(requested):
                            delay = max(1.0, requested)
                    except ValueError:
                        pass  # Без числового Retry-After используется штатный интервал.
                    await progress("polling")
                    await asyncio.sleep(delay)

        try:
            async with asyncio.timeout(wait_seconds):
                while monotonic() < deadline:
                    status = await get_json(url, 1000000)
                    state = status.get("status")
                    if state in {"completed", "completed_with_warnings"}:
                        await progress("fetching")
                        if on_complete is not None:
                            await on_complete(state)
                        payload = await get_json(url + "/result.json", settings.max_result_bytes)
                        return validator(payload)
                    if state in {"failed", "cancelled"}:
                        raise MapperError(
                            "upstream_" + state, "Задание извлечения не завершено успешно", 409
                        )
                    if state not in {"queued", "running"}:
                        raise MapperError("upstream_state", "Неизвестный статус извлечения", 502)
                    await progress("polling")
                    await asyncio.sleep(settings.poll_seconds)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            names = {404: "not_found", 410: "expired", 401: "unauthorized", 403: "forbidden"}
            raise MapperError(
                "upstream_" + names.get(code, "http_error"), "API извлечения отклонил запрос", 502
            ) from exc
        except (httpx.RequestError, TimeoutError) as exc:
            raise MapperError(
                "upstream_timeout", "Истекло время ожидания API извлечения", 503
            ) from exc
        raise MapperError("upstream_timeout", "Истекло время ожидания извлечения", 503)


class SharedEmbeddings:
    """Клиент logical shared-embedding, не создающий локальную модель и не требующий AI-ключа."""

    def __init__(self, client: httpx.AsyncClient, settings) -> None:
        """Получает переиспользуемый HTTP pool и контракт embedding endpoint."""
        self.client, self.settings = client, settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Проверяет количество, порядок, размерность и конечность чисел ответа /embeddings."""
        if not texts:
            return []
        settings = self.settings
        try:
            result = await bounded_json(
                self.client,
                "POST",
                settings.shared_embedding_base_url.rstrip("/") + "/embeddings",
                30000000,
                json={"model": settings.shared_embedding_model, "input": texts},
                timeout=settings.embedding_timeout_seconds,
            )
            entries = sorted(result["data"], key=lambda row: row["index"])
            if [row["index"] for row in entries] != list(range(len(texts))):
                raise ValueError("Порядок векторов")
            vectors = [row["embedding"] for row in entries]
            size = len(vectors[0])
            if not size or any(
                len(v) != size
                or any(
                    isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                    for x in v
                )
                or not any(v)
                for v in vectors
            ):
                raise ValueError("Некорректные векторы")
            return vectors
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MapperError(
                "embedding_unavailable", "Неверный или недоступный embedding endpoint", 503
            ) from exc

    async def identity(self) -> dict:
        """Проверяет alias, identity metadata и контрольный вектор вместо доверия имени модели."""
        settings = self.settings
        try:
            models = await bounded_json(
                self.client,
                "GET",
                settings.shared_embedding_base_url.rstrip("/") + "/models",
                1000000,
            )
            model = next(m for m in models["data"] if m["id"] == settings.shared_embedding_model)
            probe = (await self.embed(["Контроль совместимости номенклатуры DN50 PN16"]))[0]
        except (httpx.HTTPError, KeyError, StopIteration, TypeError) as exc:
            raise MapperError(
                "embedding_identity", "Не подтверждена identity модели shared-embedding", 503
            ) from exc
        identity = {
            "endpoint": settings.shared_embedding_base_url,
            "model": settings.shared_embedding_model,
            "dimension": len(probe),
            "revision": settings.embedding_revision
            or digest({key: model.get(key) for key in ("id", "root", "revision", "owned_by")}),
            "probe": digest([round(v, 6) for v in probe]),
            "preprocessing": "nfc-space-v1",
        }
        return {
            **identity,
            "fingerprint": digest(identity),
            "metadata_fingerprint": digest(
                {key: value for key, value in identity.items() if key != "probe"}
            ),
            "probe_vector": probe,
        }
