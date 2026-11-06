# src/application/catalog.py
"""Восстанавливаемая синхронизация каталога: неизменяемый staging, vector cache и atomic publish."""

from __future__ import annotations

from time import time
from uuid import uuid4

from src.application.ports import CatalogRepository, CatalogSource, Embeddings, VectorIndex
from src.domain.embedding import compatible_embedding
from src.domain.matching import normalize
from src.domain.models import MapperError, digest
from src.observability import timed


class SynchronizeCatalogUseCase:
    """Публикует проверенный снимок и возобновляет обработку с сохранённой пачки."""

    def __init__(
        self,
        source: CatalogSource,
        catalog: CatalogRepository,
        embeddings: Embeddings,
        index: VectorIndex,
        batch_size: int,
    ) -> None:
        """Принимает порты без создания конкретных SQL/HTTP клиентов внутри application."""
        self.source, self.catalog, self.embeddings, self.index = source, catalog, embeddings, index
        self.batch_size = batch_size

    @timed("catalog_synchronize")
    async def execute(self, progress) -> dict:
        """Восстанавливает совместимый staging; при отказе сохраняет прежний активный индекс."""
        try:
            async with self.catalog.lock():
                await progress("reading_xml")
                source = await self.source.read()
                identity = await self.embeddings.identity()
                previous = await self.catalog.status()
                usable = (
                    previous.get("ready")
                    and await self.index.exists(previous["collection"])
                    and await self.index.count(previous["collection"]) == previous["count"]
                )
                compatible = usable and compatible_embedding(
                    previous.get("embedding_identity", {"fingerprint": previous["fingerprint"]}),
                    identity,
                )
                if compatible and not previous.get("embedding_identity"):
                    previous["embedding_identity"] = identity
                    metadata = {
                        key: value
                        for key, value in previous.items()
                        if key not in {"ready", "version", "last_successful_sync", "last_error"}
                    }
                    await self.catalog.update_stage(previous["version"], metadata)
                if compatible and previous.get("source_hash") == source.source_hash:
                    await self.catalog.activate(previous["version"])
                    return await self.catalog.status()
                saved = await self.catalog.resumable(source.source_hash, identity)
                if saved:
                    metadata = dict(saved)
                    version = metadata.pop("version")
                    metadata.setdefault("embedding_identity", identity)
                else:
                    version = str(uuid4())
                    incremental = bool(compatible)
                    metadata = {
                        "collection": "spec_mapper_" + version.replace("-", ""),
                        "count": len(source.items),
                        "source_hash": source.source_hash,
                        "fingerprint": previous["fingerprint"]
                        if incremental
                        else identity["fingerprint"],
                        "embedding_identity": previous["embedding_identity"]
                        if incremental
                        else identity,
                        "dimension": identity["dimension"],
                        "warnings": source.warnings,
                        "statistics": source.statistics,
                        "mode": "incremental" if incremental else "full",
                        "base_version": previous["version"] if incremental else None,
                        "base_collection": previous["collection"] if incremental else None,
                        "resume_offset": 0,
                        "initialized": False,
                        "published": False,
                        "embedded_count": 0,
                        "indexed_count": 0,
                        "removed_count": 0,
                        "updated_at": time(),
                    }
                    await self.catalog.stage(version, source.items, metadata)
                await progress(
                    "preparing_index",
                    catalog_version=version,
                    base_version=metadata.get("base_version"),
                    processed=metadata["resume_offset"],
                    total=len(source.items),
                )
                if not metadata["initialized"]:
                    await self._initialize(version, metadata, source.items)
                await self._index(version, metadata, source.items, progress)
                if await self.index.count(metadata["collection"]) != len(source.items):
                    raise MapperError(
                        "index_count", "Число точек Qdrant не совпало с каталогом", 503
                    )
                if not compatible_embedding(
                    metadata["embedding_identity"], await self.embeddings.identity()
                ):
                    raise MapperError(
                        "embedding_changed", "Модель изменилась во время индексации", 503
                    )
                metadata.update(
                    reused_count=len(source.items) - metadata["embedded_count"], updated_at=time()
                )
                await self.catalog.update_stage(version, metadata)
                await progress(
                    "publishing",
                    catalog_version=version,
                    base_version=metadata.get("base_version"),
                    processed=len(source.items),
                    total=len(source.items),
                )
                async with self.catalog.guard():
                    old_collection = await self.index.switch(metadata["collection"])
                    try:
                        await self.catalog.activate(version)
                    except BaseException:
                        await self.index.restore(old_collection)
                        raise
                return await self.catalog.status()
        except MapperError as exc:
            await self.catalog.sync_error(exc.code)
            raise

    async def _initialize(self, version: str, metadata: dict, items: list) -> None:
        """Восстанавливает создание только unpublished staging; активную коллекцию не удаляет."""
        collection = metadata["collection"]
        if await self.index.exists(collection):
            await self.index.remove(collection)
        if metadata["mode"] == "incremental":
            await self.index.clone(metadata["base_collection"], collection)
            old_ids = set(await self.catalog.guids(metadata["base_version"]))
            removed = sorted(old_ids - {item.guid for item in items})
            for start in range(0, len(removed), 500):
                await self.index.delete_points(collection, removed[start : start + 500])
            metadata["removed_count"] = len(removed)
        else:
            await self.index.create(collection, metadata["dimension"])
        metadata.update(initialized=True, updated_at=time())
        await self.catalog.update_stage(version, metadata)

    async def _index(self, version: str, metadata: dict, items: list, progress) -> None:
        """Фиксирует offset после durable upsert; повтор незавершённой пачки идемпотентен."""
        for start in range(metadata["resume_offset"], len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            if metadata["mode"] == "incremental":
                old = await self.catalog.items(
                    metadata["base_version"], [item.guid for item in batch]
                )
                batch = [
                    item
                    for item in batch
                    if item.guid not in old or old[item.guid].content_hash != item.content_hash
                ]
            if batch:
                keys = [digest([metadata["fingerprint"], item.content_hash]) for item in batch]
                vectors = await self.catalog.vectors(keys)
                missing = [
                    (key, item) for key, item in zip(keys, batch, strict=True) if key not in vectors
                ]
                if missing:
                    texts = [
                        normalize(
                            " ".join((item.name, item.article, item.brand, item.manufacturer))
                        )
                        for _, item in missing
                    ]
                    embedded = await self.embeddings.embed(texts)
                    if any(len(vector) != metadata["dimension"] for vector in embedded):
                        raise MapperError(
                            "embedding_changed",
                            "Размерность модели изменилась во время импорта",
                            503,
                        )
                    new = dict(zip([key for key, _ in missing], embedded, strict=True))
                    await self.catalog.cache_vectors(new)
                    vectors.update(new)
                    metadata["embedded_count"] += len(missing)
                await self.index.put(
                    metadata["collection"],
                    [
                        {"id": item.guid, "vector": vectors[key]}
                        for key, item in zip(keys, batch, strict=True)
                    ],
                )
                metadata["indexed_count"] += len(batch)
            metadata.update(
                resume_offset=min(start + self.batch_size, len(items)), updated_at=time()
            )
            await self.catalog.update_stage(version, metadata)
            await progress(
                "indexing",
                catalog_version=version,
                base_version=metadata.get("base_version"),
                processed=metadata["resume_offset"],
                total=len(items),
            )


class PruneCatalogUseCase:
    """Собирает устаревшие версии, сохраняя active, rollback и все ссылки живых заданий."""

    def __init__(self, catalog: CatalogRepository, index: VectorIndex) -> None:
        """Получает порты собственных систем без доступа к XML или persistent volumes."""
        self.catalog, self.index = catalog, index

    @timed("catalog_prune")
    async def execute(self) -> None:
        """Удаляет только зарегистрированные obsolete snapshots под lock публикации."""
        async with self.catalog.lock():
            async with self.catalog.guard():
                for version in await self.catalog.obsolete_versions():
                    expected = "spec_mapper_" + version["version"].replace("-", "")
                    if version.get("collection") != expected:
                        raise MapperError(
                            "catalog_collection_namespace", "Некорректный namespace снимка"
                        )
                    await self.index.remove(expected)
                    await self.catalog.discard_version(version["version"])
