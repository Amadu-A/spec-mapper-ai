# src/application/catalog.py
"""Общая синхронизация XML/будущего адаптера 1С с инкрементальным embedding-кэшем."""

from __future__ import annotations

from uuid import uuid4

from src.application.ports import CatalogRepository, CatalogSource, Embeddings, VectorIndex
from src.domain.matching import normalize
from src.domain.models import MapperError, digest
from src.observability import timed


class SynchronizeCatalogUseCase:
    """Строит новый снимок отдельно от активного и публикует только проверенный индекс."""

    def __init__(
        self,
        source: CatalogSource,
        catalog: CatalogRepository,
        embeddings: Embeddings,
        index: VectorIndex,
        batch_size: int,
    ) -> None:
        """Получает заменяемые порты; ни HTTP, ни SQLAlchemy внутри use-case не создаются."""
        self.source, self.catalog, self.embeddings, self.index = source, catalog, embeddings, index
        self.batch_size = batch_size

    @timed("catalog_synchronize")
    async def execute(self, progress) -> dict:
        """Не векторизует неизменённые позиции; при сбое сохраняет старый каталог и alias."""
        try:
            async with self.catalog.lock():
                await progress("reading_xml")
                source = await self.source.read()
                identity = await self.embeddings.identity()
                previous = await self.catalog.status()
                if (
                    previous.get("source_hash") == source.source_hash
                    and previous.get("fingerprint") == identity["fingerprint"]
                ):
                    await self.catalog.activate(previous["version"])
                    return await self.catalog.status()
                version = str(uuid4())
                collection = "spec_mapper_" + version.replace("-", "")
                metadata = {
                    "collection": collection,
                    "count": len(source.items),
                    "source_hash": source.source_hash,
                    "fingerprint": identity["fingerprint"],
                    "dimension": identity["dimension"],
                    "warnings": source.warnings,
                }
                # Ledger создаётся до Qdrant, чтобы после crash остался адрес staging для очистки.
                await self.catalog.stage(version, source.items, metadata)
                incremental = previous.get("fingerprint") == identity["fingerprint"]
                removed = []
                if incremental:
                    await self.index.clone(previous["collection"], collection)
                    old_ids = set(await self.catalog.guids(previous["version"]))
                    new_ids = {item.guid for item in source.items}
                    removed = sorted(old_ids - new_ids)
                    for start in range(0, len(removed), 500):
                        await self.index.delete_points(collection, removed[start : start + 500])
                else:
                    await self.index.create(collection, identity["dimension"])
                computed = indexed = 0
                for offset in range(0, len(source.items), self.batch_size):
                    await progress("indexing")
                    batch = source.items[offset : offset + self.batch_size]
                    if incremental:
                        old = await self.catalog.items(
                            previous["version"], [item.guid for item in batch]
                        )
                        batch = [
                            item
                            for item in batch
                            if item.guid not in old
                            or old[item.guid].content_hash != item.content_hash
                        ]
                    if not batch:
                        continue
                    keys = [digest([identity["fingerprint"], item.content_hash]) for item in batch]
                    vectors = await self.catalog.vectors(keys)
                    missing = [
                        (key, item)
                        for key, item in zip(keys, batch, strict=True)
                        if key not in vectors
                    ]
                    if missing:
                        texts = [
                            normalize(
                                " ".join((item.name, item.article, item.brand, item.manufacturer))
                            )
                            for _, item in missing
                        ]
                        embedded = await self.embeddings.embed(texts)
                        if any(len(v) != identity["dimension"] for v in embedded):
                            raise MapperError(
                                "embedding_changed",
                                "Размерность модели изменилась во время импорта",
                                503,
                            )
                        new = dict(zip([key for key, _ in missing], embedded, strict=True))
                        await self.catalog.cache_vectors(new)
                        vectors.update(new)
                        computed += len(missing)
                    await self.index.put(
                        collection,
                        [
                            {"id": item.guid, "vector": vectors[key]}
                            for key, item in zip(keys, batch, strict=True)
                        ],
                    )
                    indexed += len(batch)
                if await self.index.count(collection) != len(source.items):
                    raise MapperError(
                        "index_count", "Число точек Qdrant не совпало с каталогом", 503
                    )
                metadata = {
                    "collection": collection,
                    "count": len(source.items),
                    "source_hash": source.source_hash,
                    "fingerprint": identity["fingerprint"],
                    "dimension": identity["dimension"],
                    "warnings": source.warnings,
                    "embedded_count": computed,
                    "reused_count": len(source.items) - computed,
                    "indexed_count": indexed,
                    "removed_count": len(removed),
                    "mode": "incremental" if incremental else "full",
                }
                await self.catalog.update_stage(version, metadata)
                await progress("publishing")
                async with self.catalog.guard():
                    old_collection = await self.index.switch(collection)
                    try:
                        await self.catalog.activate(version)
                    except BaseException:
                        await self.index.restore(old_collection)
                        raise
                return await self.catalog.status()
        except MapperError as exc:
            await self.catalog.sync_error(exc.code)
            raise


class PruneCatalogUseCase:
    """Собирает только устаревшие версии каталога, сохраняя active, rollback и ссылки живых jobs."""

    def __init__(self, catalog: CatalogRepository, index: VectorIndex) -> None:
        """Получает порты тех же private систем без доступа к XML и volumes."""
        self.catalog, self.index = catalog, index

    @timed("catalog_prune")
    async def execute(self) -> None:
        """Удаляет только зарегистрированные project snapshots старше часа, под advisory lock."""
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
