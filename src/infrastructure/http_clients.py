# src/infrastructure/http_clients.py
"""Ограниченные HTTP-клиенты upstream и embedding с безопасными предметными ошибками."""

from __future__ import annotations

import asyncio
import json
import math
from time import monotonic

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

    async def fetch(self, source_job_id: str, progress) -> dict:
        """Различает terminal ошибки, ждёт финал и ровно один раз скачивает result.json."""
        settings = self.settings
        if not settings.extracting_api_key.get_secret_value():
            raise MapperError("upstream_key_missing", "Не задан серверный API-ключ извлечения", 503)
        url = f"{settings.extracting_base_url.rstrip('/')}/api/v1/extractions/{source_job_id}"
        headers = {"X-API-Key": settings.extracting_api_key.get_secret_value()}
        deadline = monotonic() + settings.upstream_timeout_seconds
        try:
            async with asyncio.timeout(settings.upstream_timeout_seconds):
                while monotonic() < deadline:
                    status = await bounded_json(self.client, "GET", url, 1000000, headers=headers)
                    state = status.get("status")
                    if state in {"completed", "completed_with_warnings"}:
                        await progress("fetching")
                        payload = await bounded_json(
                            self.client,
                            "GET",
                            url + "/result.json",
                            settings.max_result_bytes,
                            headers=headers,
                        )
                        return validate_source(payload, settings.max_items)
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
