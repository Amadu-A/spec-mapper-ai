# src/infrastructure/qdrant.py
"""Асинхронный REST-адаптер частного Qdrant со staging коллекциями и alias rollback."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from src.domain.models import MapperError
from src.infrastructure.http_clients import bounded_json


class QdrantIndex:
    """Сохраняет активные индексы доступными во время построения следующего снимка."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, alias: str) -> None:
        """Привязывает ограниченный HTTP pool к project-specific endpoint и alias."""
        self.client, self.base_url, self.alias = client, base_url.rstrip("/"), alias

    async def request(self, method: str, path: str, **kwargs) -> dict:
        """Преобразует сетевые ошибки Qdrant в безопасный код зависимости."""
        try:
            return await bounded_json(self.client, method, self.base_url + path, 20000000, **kwargs)
        except httpx.HTTPError as exc:
            raise MapperError(
                "qdrant_unavailable", "Частный индекс Qdrant недоступен", 503
            ) from exc

    async def create(self, collection: str, dimension: int) -> None:
        """Создаёт только новую коллекцию; recreate/delete активной запрещён."""
        await self.request(
            "PUT",
            f"/collections/{collection}",
            json={"vectors": {"size": dimension, "distance": "Cosine"}},
        )

    async def exists(self, collection: str) -> bool:
        """Читает существование коллекции без её изменения."""
        return (await self.request("GET", f"/collections/{collection}/exists"))["result"]["exists"]

    async def clone(self, source: str, target: str) -> None:
        """Восстанавливает новую коллекцию из snapshot вместе с готовым индексом HNSW."""
        exists = await self.request("GET", f"/collections/{target}/exists")
        if exists["result"]["exists"]:
            raise MapperError("index_target_exists", "Staging коллекция уже существует", 409)
        snapshot = (
            await self.request("POST", f"/collections/{source}/snapshots?wait=true", timeout=300)
        )["result"]
        name = quote(snapshot["name"], safe="")
        path = f"/collections/{source}/snapshots/{name}"
        try:
            await self.request(
                "PUT",
                f"/collections/{target}/snapshots/recover?wait=true",
                json={
                    "location": self.base_url + path,
                    "priority": "snapshot",
                    "checksum": snapshot.get("checksum"),
                },
                timeout=300,
            )
        finally:
            await self.request("DELETE", path + "?wait=true", timeout=300)

    async def delete_points(self, collection: str, ids: list[str]) -> None:
        """Удаляет устаревшие UUID пачкой из ещё не опубликованной коллекции."""
        if ids:
            await self.request(
                "POST", f"/collections/{collection}/points/delete?wait=true", json={"points": ids}
            )

    async def put(self, collection: str, points: list[dict]) -> None:
        """Ждёт фиксации пачки UUID-точек перед проверкой count."""
        await self.request(
            "PUT", f"/collections/{collection}/points?wait=true", json={"points": points}
        )

    async def count(self, collection: str) -> int:
        """Возвращает точное число проиндексированных позиций."""
        value = await self.request(
            "POST", f"/collections/{collection}/points/count", json={"exact": True}
        )
        return value["result"]["count"]

    async def remove(self, collection: str) -> None:
        """Удаляет только явно переданную устаревшую коллекцию; повторный 404 допустим."""
        try:
            await bounded_json(
                self.client, "DELETE", self.base_url + "/collections/" + collection, 1000000
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise MapperError(
                    "qdrant_cleanup", "Не удалось удалить устаревший индекс", 503
                ) from exc

    async def switch(self, collection: str) -> str | None:
        """Одним Qdrant запросом меняет alias; старую коллекцию сохраняет для jobs и rollback."""
        aliases = (await self.request("GET", "/aliases"))["result"]["aliases"]
        old = next((a["collection_name"] for a in aliases if a["alias_name"] == self.alias), None)
        actions = [{"delete_alias": {"alias_name": self.alias}}] if old else []
        actions.append({"create_alias": {"collection_name": collection, "alias_name": self.alias}})
        await self.request("POST", "/collections/aliases", json={"actions": actions})
        return old

    async def restore(self, collection: str | None) -> None:
        """Возвращает предыдущий alias либо удаляет только новый alias при первом импорте."""
        if collection:
            await self.switch(collection)
        else:
            await self.request(
                "POST",
                "/collections/aliases",
                json={
                    "actions": [
                        {"delete_alias": {"alias_name": self.alias}},
                    ]
                },
            )

    async def search(
        self, collection: str, vectors: list[list[float]], limit: int
    ) -> list[list[tuple[str, float]]]:
        """Ищет Top-K пачкой по закреплённой коллекции, исключая гонку смены alias."""
        value = await self.request(
            "POST",
            f"/collections/{collection}/points/query/batch",
            json={
                "searches": [
                    {"query": vector, "limit": limit, "with_payload": False} for vector in vectors
                ],
            },
        )
        results = value["result"]
        if len(results) != len(vectors):
            raise MapperError(
                "qdrant_contract", "Число ответов Top-K не совпадает с числом строк", 502
            )
        return [
            [(str(point["id"]), float(point["score"])) for point in row["points"]]
            for row in results
        ]
