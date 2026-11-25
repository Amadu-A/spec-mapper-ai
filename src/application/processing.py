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
from src.domain.invoices.adapter import invoice_items, matchable, matching_source
from src.domain.invoices.models import validate_invoice
from src.domain.lifecycle import safe_upstream
from src.domain.matching import choose, rerank, specification_text
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
        catalog_sources=None,
        catalog_management=None,
        catalog_events=None,
        invoice_extraction=None,
    ) -> None:
        """Принимает все порты через composition root без создания клиентов в use-case."""
        self.jobs, self.catalog, self.extraction = jobs, catalog, extraction
        self.catalog_sources = catalog_sources
        self.catalog_management = catalog_management
        self.catalog_events = catalog_events
        self.invoice_extraction = invoice_extraction
        self.embeddings, self.index, self.synchronizer, self.settings = (
            embeddings,
            index,
            synchronizer,
            settings,
        )

    @timed("job_process")
    async def process(self, job_id: str, delivery_generation: int | None = None) -> str:
        """Возобновляет сохранённый прогресс; старый worker прекращает работу при потере lease."""
        job = await self.jobs.claim(job_id, delivery_generation)
        if job is None:
            return "noop"
        token = job["lease_token"]
        lease_lost = False

        async def checkpoint(**changes) -> None:
            """Фиксирует только прогресс текущего fence; потеря lease останавливает pipeline."""
            if not await self.jobs.checkpoint(job_id, token, **changes):
                raise MapperError("lease_lost", "Задание передано другому worker", 409)

        async def progress(stage: str, **details) -> None:
            """Отражает текущий этап без пользовательского payload в логах и broker message."""
            if "upstream_progress" in details:
                safe = safe_upstream(details["upstream_progress"])
                job["data"]["upstream_progress"] = safe
                await checkpoint(
                    stage="upstream_queued" if safe.get("status") == "queued" else "extracting",
                    state="extracting",
                    data=job["data"],
                )
            elif details:
                if "downloaded_bytes" in details:
                    job["data"]["downloaded_bytes"] = details["downloaded_bytes"]
                job["data"].update(catalog_progress=details)
                if details.get("catalog_version"):
                    job["data"]["catalog_version"] = details["catalog_version"]
                await checkpoint(stage=stage, state=stage, data=job["data"])
            else:
                await checkpoint(stage=stage, state=stage)

        async def execute() -> None:
            """Выбирает только вид durable операции и сохраняет terminal checkpoint."""
            if job.get("cancel_requested"):
                await self._cancel(job, checkpoint)
                return
            if job["attempts"] > self.settings.max_attempts:
                await checkpoint(state="failed", stage="failed", error_code="attempts_exhausted")
                return
            if job["kind"] == "catalog_events":
                status = await self.catalog_events.execute(job, progress)
                await checkpoint(
                    state="complete",
                    stage="complete",
                    data={**job["data"], "catalog_status": status},
                    error_code=None,
                )
                return
            if job["kind"] == "catalog":
                config = job["data"].get("catalog_sync", {})
                if self.catalog_sources and config.get("source_type") == "http_onec":
                    status = await self.synchronizer.execute(
                        progress, source_override=self.catalog_sources.source(config, progress)
                    )
                    if self.catalog_management:
                        await self.catalog_management.record_success(config, status)
                else:
                    status = await self.synchronizer.execute(progress)
                await checkpoint(
                    state="complete",
                    stage="complete",
                    data={
                        **job["data"],
                        "catalog_status": status,
                        "catalog_progress": {
                            **job["data"].get("catalog_progress", {}),
                            "processed": status["count"],
                            "total": status["count"],
                        },
                    },
                    error_code=None,
                )
                return
            await self._match(job, checkpoint, progress)

        running = asyncio.create_task(execute())

        async def heartbeat() -> None:
            """Отменяет обработку, если lease истёк либо другой worker получил новый fence."""
            nonlocal lease_lost
            while True:
                await asyncio.sleep(self.settings.lease_seconds / 3)
                if not await self.jobs.heartbeat(job_id, token):
                    lease_lost = True
                    running.cancel()
                    return

        pulse = asyncio.create_task(heartbeat())
        try:
            await running
        except asyncio.CancelledError:
            if not lease_lost:
                raise
            return "fenced"
        except MapperError as exc:
            if exc.code == "lease_lost":
                return "fenced"
            busy = exc.code == "catalog_busy"
            if job["kind"] == "catalog_events" and self.catalog_events and not busy:
                await self.catalog_events.repository.failure(job, exc.code)
            retry = busy or (exc.status >= 500 and job["attempts"] < self.settings.max_attempts)
            if retry:
                await asyncio.sleep(min(2 ** job["attempts"], 10))
            await self.jobs.checkpoint(
                job_id,
                token,
                state="queued" if retry else "failed",
                stage="retry" if retry else "failed",
                error_code=exc.code,
                **({"attempts": max(0, job["attempts"] - 1)} if busy else {}),
            )
            logging.getLogger(__name__).warning(
                "job_error", extra={"job_id": job_id, "error_code": exc.code}
            )
        except Exception:
            if job["kind"] == "catalog_events" and self.catalog_events:
                await self.catalog_events.repository.failure(job, "internal_error")
            await self.jobs.checkpoint(
                job_id, token, state="failed", stage="failed", error_code="internal_error"
            )
            logging.getLogger(__name__).error("job_unexpected_error", extra={"job_id": job_id})
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        return "handled"

    async def _extract_pdf(self, job: dict, checkpoint, progress) -> None:
        """Сохраняет внешние ID до следующего шага для возобновления после restart."""
        data, spec = job["data"], job["data"]["specification"]
        await progress("extracting")
        if not data.get("upstream_document_id"):
            content = await self.jobs.pdf(job["id"])
            data["upstream_document_id"] = await self.extraction.upload(content, spec["filename"])
            await checkpoint(data=data, state="extracting", stage="extracting")
        if not data.get("source_job_id"):

            async def submit():
                """Использует тот же ключ и ранее сохранённый document ID при обычном recovery."""
                return await self.extraction.start(
                    data["upstream_document_id"], spec["pages"], spec["project_code"], job["id"]
                )

            data["source_job_id"] = await self._submit_managed(job, checkpoint, submit)
            await checkpoint(data=data, state="extracting", stage="extracting")

    async def _submit_managed(self, job: dict, checkpoint, submit) -> str:
        """Intent до POST и durable поздний ответ; неизвестная отмена не создаёт новый POST."""
        data = job["data"]
        operation = digest({"job_id": job["id"], "operation": "upstream-submit"})
        data["submission"] = {"token": operation, "phase": "sending"}
        await checkpoint(data=data, state="extracting", stage="transferring")

        async def accepted():
            """Сохраняет известный UUID даже если normal pipeline уже потерял fence из-за cancel."""
            identifier = await submit()
            await self.jobs.submission_accepted(job["id"], operation, identifier)
            data["submission"]["phase"] = "accepted"
            return identifier

        task = asyncio.create_task(accepted())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Завершаем bounded request и запись ID; SIGKILL оставляет неопределённый intent.
            await task
            raise

    async def _cancel(self, job: dict, checkpoint) -> None:
        """Отмена managed операции отдельна от нормального retry и не запускает извлечение."""
        data = job["data"]
        identifier = data.get("source_job_id")
        if not identifier:
            await checkpoint(
                state="cancelling",
                stage="cancel_unknown",
                error_code="upstream_submission_unknown",
                retry_after=30,
            )
            return
        extraction = self.invoice_extraction if job["kind"] == "invoice" else self.extraction
        try:
            result = await extraction.cancel(identifier)
        except MapperError as error:
            await checkpoint(
                state="cancelling",
                stage="cancel_retry",
                error_code=error.code,
                retry_after=min(60, 2 ** min(job["attempts"], 6)),
            )
            return
        data["upstream_progress"] = safe_upstream(result)
        stopped = result["status"] in {
            "cancelled",
            "completed",
            "completed_with_warnings",
            "failed",
        }
        data["cancellation_outcome"] = result["status"]
        await checkpoint(
            data=data,
            state="cancelled" if stopped else "cancelling",
            stage="cancelled" if stopped else "cancelling",
            error_code=None,
            retry_after=self.settings.poll_seconds,
        )

    async def _match(self, job: dict, checkpoint, progress) -> None:
        """Переиспользует source snapshot и пачки после restart; исходные дубли не схлопывает."""
        data = job["data"]
        invoice = job["kind"] == "invoice"

        async def active():
            """Предотвращает новые внешние операции после принятой отмены или утраты lease."""
            if not await self.jobs.still_running(job["id"], job["lease_token"]):
                raise MapperError("lease_lost", "Обработка остановлена", 409)

        await active()
        if invoice and data.get("source") is None:
            await self._extract_invoice(job, checkpoint, progress)
        if data.get("source") is None and data.get("specification"):
            await self._extract_pdf(job, checkpoint, progress)
        if data.get("source") is None:
            data["source"] = await self.extraction.fetch(data["source_job_id"], progress)
            data["source_hash"] = digest(data["source"])
            await checkpoint(data=data, state="matching", stage="matching")
        data["source_hash"] = digest(data["source"])
        await active()
        async with self.catalog.guard():
            current = await self.catalog.status()
            if not current["ready"]:
                raise MapperError("catalog_not_ready", "Сначала импортируйте номенклатуру", 503)
            await active()
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
        entries = list(invoice_items(data["source"])) if invoice else None
        items = [entry[2] for entry in entries] if invoice else data["source"]["items"]
        batch_size = self.settings.embedding_batch_size
        for start in range(len(data["rows"]), len(items), batch_size):
            batch = items[start : start + batch_size]
            derived = (
                [matching_source(row) for row in batch if matchable(row)] if invoice else batch
            )
            matched = iter(await self._match_batch(data, derived, active))
            for offset, source in enumerate(batch):
                if not invoice:
                    data["rows"].append(next(matched))
                    continue
                payment = source.get("row_kind") in {"advance_payment", "final_payment"}
                row = (
                    next(matched)
                    if matchable(source)
                    else {
                        "selection": None,
                        "status": "not_applicable" if payment else "review",
                        "confirmed": False,
                        "manually_reviewed": False,
                        "candidates": [],
                    }
                )
                warnings = []
                if not matchable(source):
                    warnings.append("payment_row" if payment else "unsupported_row_kind")
                if source.get("row_kind") == "service":
                    warnings.append("service_requires_review")
                if row["selection"] and not row["selection"].get("onec_guid"):
                    warnings.append("onec_guid_missing")
                if source.get("requires_manual_review"):
                    warnings.append("source_requires_review")
                if warnings and row["status"] == "matched":
                    row["status"] = "review"
                data["rows"].append(
                    {
                        **row,
                        "warnings": warnings,
                        "reference": entries[start + offset][3],
                        "row_kind": source.get("row_kind", "product"),
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

    async def _match_batch(self, data: dict, sources: list[dict], active=None) -> list[dict]:
        """Единственная реализация embeddings/Top-K/rerank/choose для обоих профилей."""
        if not sources:
            return []
        if active:
            await active()
        vectors = await self.embeddings.embed([specification_text(row) for row in sources])
        if active:
            await active()
        hits = await self.index.search(data["collection"], vectors, self.settings.top_k)
        metadata = await self.catalog.items(
            data["catalog_version"], [guid for row in hits for guid, _ in row]
        )
        rows = []
        for source, found in zip(sources, hits, strict=True):
            candidates = rerank(
                source,
                [
                    (metadata[guid], score)
                    for guid, score in found
                    if guid in metadata and metadata[guid].active
                ],
            )
            rows.append(
                {
                    **choose(candidates, self.settings.match_threshold, self.settings.match_margin),
                    "candidates": [candidate.to_dict() for candidate in candidates],
                    "manually_reviewed": False,
                }
            )
        return rows

    async def _extract_invoice(self, job: dict, checkpoint, progress) -> None:
        """Возобновляет пакет 8083 по durable ID/ключу; исходный JSON хранит неизменным."""
        data = job["data"]
        if self.invoice_extraction is None:
            raise MapperError("upstream_unavailable", "Не подключён профиль извлечения счетов", 503)
        await progress("extracting")
        if not data.get("source_job_id"):
            content = await self.jobs.invoice_files(job["id"])

            async def submit():
                """Передаёт исходный пакет со стабильным ключом, без второго extractor pipeline."""
                return await self.invoice_extraction.submit(data["invoice"], content, job["id"])

            data["source_job_id"] = await self._submit_managed(job, checkpoint, submit)
            await checkpoint(data=data, state="extracting", stage="extracting")

        async def invoice_progress(stage: str, **details) -> None:
            """Сохраняет предупреждение upstream отдельно от исходника и каталога."""
            if details.get("upstream_status"):
                data["upstream_status"] = details["upstream_status"]
                await checkpoint(data=data, state="extracting", stage=stage)
            else:
                await progress(stage, **details)

        source = await self.invoice_extraction.fetch(data["source_job_id"], invoice_progress)
        validate_invoice(source, self.settings.invoice_max_items)
        expected = data["invoice"]["files"]
        if len(source["files"]) != len(expected) or any(
            file["source_sha256"] != metadata["sha256"]
            or file["page_count"] != metadata["page_count"]
            for file, metadata in zip(source["files"], expected, strict=True)
        ):
            raise MapperError(
                "invalid_invoice_source", "Ответ не соответствует принятому пакету", 502
            )
        data["source"] = source
        data["source_hash"] = digest(source)
        await checkpoint(data=data, state="matching", stage="matching")
