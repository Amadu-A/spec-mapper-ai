# src/application/catalog.py
"""Восстанавливаемая синхронизация каталога: неизменяемый staging, vector cache и atomic publish."""

from __future__ import annotations

from time import time
from uuid import uuid4

from src.application.ports import CatalogRepository, CatalogSource, Embeddings, VectorIndex
from src.domain.embedding import compatible_embedding
from src.domain.matching import catalog_text
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
    async def execute(self, progress, source_override: CatalogSource | None = None) -> dict:
        """Восстанавливает совместимый staging; при отказе сохраняет прежний активный индекс."""
        try:
            async with self.catalog.lock():
                await progress("fetching" if source_override else "reading_xml")
                if not source_override:
                    await progress("validating")
                source = await (source_override or self.source).read()
                previous = await self.catalog.status()
                if source.context.get("full_json"):
                    minimum = source.context.get("min_items", 1)
                    shrink = source.context.get("max_shrink_ratio", 0.1)
                    if len(source.items) < minimum or (
                        previous.get("ready")
                        and len(source.items) < previous["count"] * (1 - shrink)
                        and source.context.get("shrink_ack") != source.source_hash
                    ):
                        raise MapperError(
                            "catalog_shrink_guard",
                            "Подозрительно малый JSON: проверьте выгрузку и подтвердите SHA-256",
                            422,
                        )
                await self.catalog.reconcile(source)
                await progress(
                    "validated",
                    total=len(source.items),
                    processed=0,
                    source_hash=source.source_hash,
                    statistics=source.statistics,
                )
                identity = await self.embeddings.identity()
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
                if (
                    compatible
                    and previous.get("source_hash") == source.source_hash
                    and not source.context.get("event_context")
                ):
                    if source.context.get("full_json"):
                        refreshed = {
                            key: value
                            for key, value in previous.items()
                            if key not in {"ready", "version", "last_successful_sync", "last_error"}
                        }
                        refreshed.update(
                            _full_json=True,
                            _snapshot_at=source.context.get("snapshot_at", time()),
                            _present_guids=[item.onec_guid for item in source.records],
                        )
                        await self.catalog.update_stage(previous["version"], refreshed)
                    await self.catalog.activate(previous["version"])
                    return {
                        **await self.catalog.status(),
                        "run_statistics": {
                            "added_count": 0,
                            "changed_count": 0,
                            "metadata_only_count": 0,
                            "removed_count": 0,
                            "unchanged_count": len(source.items),
                            "embedded_count": 0,
                            "indexed_count": 0,
                            "reused_count": len(source.items),
                            "counts_complete": True,
                        },
                    }
                saved = await self.catalog.resumable(source.source_hash, identity)
                if saved:
                    metadata = dict(saved)
                    version = metadata.pop("version")
                    metadata.setdefault("embedding_identity", identity)
                    metadata.setdefault("counts_complete", "added_count" in metadata)
                    metadata.setdefault("comparison_version", previous.get("version"))
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
                        "comparison_version": previous.get("version"),
                        "base_collection": previous["collection"] if incremental else None,
                        "resume_offset": 0,
                        "initialized": False,
                        "published": False,
                        "embedded_count": 0,
                        "indexed_count": 0,
                        "removed_count": 0,
                        "added_count": 0,
                        "changed_count": 0,
                        "metadata_only_count": 0,
                        "unchanged_count": 0,
                        "counts_complete": True,
                        "updated_at": time(),
                    }
                    await progress(
                        "staging",
                        catalog_version=version,
                        processed=0,
                        total=len(source.items),
                        statistics=source.statistics,
                    )
                    await self.catalog.stage(version, source.items, metadata)
                if source.records:
                    metadata["_present_guids"] = [
                        item.onec_guid for item in source.records if item.onec_guid
                    ]
                if source.context.get("full_json"):
                    metadata["_full_json"] = True
                    metadata["_snapshot_at"] = source.context.get("snapshot_at", time())
                if source.context.get("event_context"):
                    metadata["_event_context"] = source.context["event_context"]
                await self.catalog.update_stage(version, metadata)
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
                if verify := getattr(source_override, "before_publish", None):
                    await verify()
                async with self.catalog.guard():
                    old_collection = await self.index.switch(metadata["collection"])
                    try:
                        await self.catalog.activate(version)
                    except BaseException:
                        await self.index.restore(old_collection)
                        raise
                return {
                    **await self.catalog.status(),
                    "run_statistics": {
                        key: metadata.get(key, 0)
                        for key in (
                            "added_count",
                            "changed_count",
                            "metadata_only_count",
                            "removed_count",
                            "unchanged_count",
                            "embedded_count",
                            "indexed_count",
                            "reused_count",
                            "counts_complete",
                        )
                    },
                }
        except MapperError as exc:
            await self.catalog.sync_error(exc.code)
            raise

    async def _initialize(self, version: str, metadata: dict, items: list) -> None:
        """Восстанавливает создание только unpublished staging; активную коллекцию не удаляет."""
        collection = metadata["collection"]
        if await self.index.exists(collection):
            await self.index.remove(collection)
        removed = []
        if metadata.get("comparison_version"):
            old_ids = set(await self.catalog.guids(metadata["comparison_version"]))
            removed = sorted(old_ids - {item.guid for item in items})
        metadata["removed_count"] = len(removed)
        if metadata["mode"] == "incremental":
            await self.index.clone(metadata["base_collection"], collection)
            for start in range(0, len(removed), 500):
                await self.index.delete_points(collection, removed[start : start + 500])
        else:
            await self.index.create(collection, metadata["dimension"])
        metadata.update(initialized=True, updated_at=time())
        await self.catalog.update_stage(version, metadata)

    async def _index(self, version: str, metadata: dict, items: list, progress) -> None:
        """Фиксирует offset после durable upsert; повтор незавершённой пачки идемпотентен."""
        for start in range(metadata["resume_offset"], len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            for name in ("added_count", "changed_count", "unchanged_count", "metadata_only_count"):
                metadata.setdefault(name, 0)
            old = {}
            if metadata.get("comparison_version"):
                old = await self.catalog.items(
                    metadata["comparison_version"], [item.guid for item in batch]
                )
                metadata["added_count"] += sum(item.guid not in old for item in batch)
                metadata["changed_count"] += sum(
                    item.guid in old and old[item.guid].content_hash != item.content_hash
                    for item in batch
                )
                metadata["unchanged_count"] += sum(
                    item.guid in old and old[item.guid].content_hash == item.content_hash
                    for item in batch
                )
                metadata["metadata_only_count"] += sum(
                    item.guid in old
                    and old[item.guid].content_hash != item.content_hash
                    and catalog_text(old[item.guid]) == catalog_text(item)
                    for item in batch
                )
                # V3 использовал hash всей карточки. Перенос кэша разрешён только при том же input.
                migration_keys = {
                    digest([metadata["fingerprint"], old[item.guid].legacy_content_hash]): digest(
                        [metadata["fingerprint"], "catalog-normalize-v1", catalog_text(item)]
                    )
                    for item in batch
                    if item.guid in old and catalog_text(old[item.guid]) == catalog_text(item)
                }
                migrated = await self.catalog.vectors(list(migration_keys))
                if migrated:
                    targets = {migration_keys[key]: vector for key, vector in migrated.items()}
                    known = await self.catalog.vectors(list(targets))
                    await self.catalog.cache_vectors(
                        {key: value for key, value in targets.items() if key not in known}
                    )
                if metadata["mode"] == "incremental":
                    batch = [
                        item
                        for item in batch
                        if item.guid not in old
                        or catalog_text(old[item.guid]) != catalog_text(item)
                    ]
            else:
                metadata["added_count"] += len(batch)
            if batch:
                keys = [
                    digest([metadata["fingerprint"], "catalog-normalize-v1", catalog_text(item)])
                    for item in batch
                ]
                vectors = await self.catalog.vectors(keys)
                legacy_keys = {}
                for key, item in zip(keys, batch, strict=True):
                    prior = old.get(item.guid, item)
                    if key not in vectors and catalog_text(prior) == catalog_text(item):
                        legacy_keys[
                            digest([metadata["fingerprint"], prior.legacy_content_hash])
                        ] = key
                legacy = await self.catalog.vectors(list(legacy_keys))
                promoted = {legacy_keys[key]: value for key, value in legacy.items()}
                if promoted:
                    await self.catalog.cache_vectors(promoted)
                    vectors.update(promoted)
                missing = list(
                    {
                        key: item
                        for key, item in zip(keys, batch, strict=True)
                        if key not in vectors
                    }.items()
                )
                if missing:
                    texts = [catalog_text(item) for _, item in missing]
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
