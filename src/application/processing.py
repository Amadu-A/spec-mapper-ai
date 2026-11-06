# src/application/processing.py
"""Durable state machine worker: polling, получение snapshot, batched matching и checkpoint."""

from __future__ import annotations

import asyncio
import logging

from src.application.ports import (
    CatalogRepository,
    Embeddings,
    ExtractionSource,
    JobRepository,
    VectorIndex,
)
from src.domain.embedding import compatible_embedding
from src.domain.matching import choose, normalize, rerank
from src.domain.models import ALGORITHM_VERSION, MapperError, digest
from src.observability import timed


class JobProcessor:
    """Исполняет at-least-once задания; checkpoint и heartbeat защищены fencing токеном."""

    def __init__(
        self,
        jobs: JobRepository,
        catalog: CatalogRepository,
        extraction: ExtractionSource,
        embeddings: Embeddings,
        index: VectorIndex,
        synchronizer,
        settings,
    ) -> None:
        """Принимает все порты через composition root без создания клиентов в use-case."""
        self.jobs, self.catalog, self.extraction = jobs, catalog, extraction
        self.embeddings, self.index, self.synchronizer, self.settings = (
            embeddings,
            index,
            synchronizer,
            settings,
        )

    @timed("job_process")
    async def process(self, job_id: str) -> None:
        """Возобновляет сохранённый прогресс; старый worker прекращает работу при потере lease."""
        job = await self.jobs.claim(job_id)
        if job is None:
            return
        token = job["lease_token"]

        async def checkpoint(**changes) -> None:
            """Фиксирует только прогресс текущего fence; потеря lease останавливает pipeline."""
            if not await self.jobs.checkpoint(job_id, token, **changes):
                raise MapperError("lease_lost", "Задание передано другому worker", 409)

        async def progress(stage: str, **details) -> None:
            """Отражает текущий этап без пользовательского payload в логах и broker message."""
            if details:
                job["data"].update(catalog_progress=details)
                if details.get("catalog_version"):
                    job["data"]["catalog_version"] = details["catalog_version"]
                await checkpoint(stage=stage, state=stage, data=job["data"])
            else:
                await checkpoint(stage=stage, state=stage)

        async def execute() -> None:
            """Выбирает только вид durable операции и сохраняет terminal checkpoint."""
            if job["attempts"] > self.settings.max_attempts:
                await checkpoint(state="failed", stage="failed", error_code="attempts_exhausted")
                return
            if job["kind"] == "catalog":
                status = await self.synchronizer.execute(progress)
                await checkpoint(
                    state="complete",
                    stage="complete",
                    data={**job["data"], "catalog_status": status},
                )
                return
            await self._match(job, checkpoint, progress)

        running = asyncio.create_task(execute())

        async def heartbeat() -> None:
            """Отменяет обработку, если lease истёк либо другой worker получил новый fence."""
            while True:
                await asyncio.sleep(self.settings.lease_seconds / 3)
                if not await self.jobs.heartbeat(job_id, token):
                    running.cancel()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            await running
        except MapperError as exc:
            retry = exc.status >= 500 and job["attempts"] < self.settings.max_attempts
            if retry:
                await asyncio.sleep(min(2 ** job["attempts"], 10))
            await self.jobs.checkpoint(
                job_id,
                token,
                state="queued" if retry else "failed",
                stage="retry" if retry else "failed",
                error_code=exc.code,
            )
            logging.getLogger(__name__).warning(
                "job_error", extra={"job_id": job_id, "error_code": exc.code}
            )
        except Exception:
            await self.jobs.checkpoint(
                job_id, token, state="failed", stage="failed", error_code="internal_error"
            )
            logging.getLogger(__name__).error("job_unexpected_error", extra={"job_id": job_id})
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    async def _match(self, job: dict, checkpoint, progress) -> None:
        """Переиспользует source snapshot и пачки после restart; исходные дубли не схлопывает."""
        data = job["data"]
        if data.get("source") is None:
            data["source"] = await self.extraction.fetch(data["source_job_id"], progress)
            data["source_hash"] = digest(data["source"])
            await checkpoint(data=data, state="matching", stage="matching")
        data["source_hash"] = digest(data["source"])
        async with self.catalog.guard():
            current = await self.catalog.status()
            if not current["ready"]:
                raise MapperError("catalog_not_ready", "Сначала импортируйте номенклатуру", 503)
            identity = await self.embeddings.identity()
            expected = data.get("embedding_identity")
            if expected is None:
                expected = (
                    {"fingerprint": data["catalog_fingerprint"]}
                    if data.get("catalog_fingerprint")
                    else current.get("embedding_identity", {"fingerprint": current["fingerprint"]})
                )
            if not compatible_embedding(expected, identity):
                raise MapperError(
                    "embedding_incompatible", "Модель изменилась: требуется новая индексация", 503
                )
            data.setdefault("catalog_version", current["version"])
            data.setdefault("catalog_fingerprint", current["fingerprint"])
            data.setdefault("embedding_identity", expected)
            data.setdefault("collection", current["collection"])
            data["algorithm_version"] = ALGORITHM_VERSION
            await checkpoint(data=data, state="matching", stage="matching")
        items, batch_size = data["source"]["items"], self.settings.embedding_batch_size
        for start in range(len(data["rows"]), len(items), batch_size):
            batch = items[start : start + batch_size]
            vectors = await self.embeddings.embed(
                [normalize(row["Caption"]) or " " for row in batch]
            )
            hits = await self.index.search(data["collection"], vectors, self.settings.top_k)
            ids = [guid for row in hits for guid, _ in row]
            metadata = await self.catalog.items(data["catalog_version"], ids)
            for source, found in zip(batch, hits, strict=True):
                candidates = rerank(
                    source,
                    [
                        (metadata[guid], score)
                        for guid, score in found
                        if guid in metadata and metadata[guid].active
                    ],
                )
                selected = choose(
                    candidates, self.settings.match_threshold, self.settings.match_margin
                )
                data["rows"].append(
                    {
                        **selected,
                        "candidates": [c.to_dict() for c in candidates],
                        "manually_reviewed": False,
                    }
                )
            await checkpoint(data=data, state="matching", stage="matching")
        uncertain = any(row["status"] in {"review", "not_found"} for row in data["rows"])
        await checkpoint(
            data=data,
            state="ready_for_review" if uncertain else "complete",
            stage="complete",
            error_code=None,
            revision=job["revision"] + 1,
        )
