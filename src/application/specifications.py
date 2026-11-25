# src/application/specifications.py
"""Единый процесс PDF → проверка → утверждение поверх общей durable очереди Mapper.

PDF и задание фиксируются одной транзакцией до ответа 202. Внутренние идентификаторы
извлечения остаются в worker; клиенту достаточно одного ID и одного API-ключа.
"""

from __future__ import annotations

import hashlib
import re

from src.application.mappings import public_status, result_page
from src.application.ports import JobRepository
from src.domain.models import MapperError
from src.observability import timed


class Specifications:
    """Принимает PDF и ограничивает единый контракт заданиями нового процесса."""

    def __init__(self, jobs: JobRepository, mappings, max_upload_bytes: int) -> None:
        """Получает общие операции ревью и репозиторий, не создавая внешних клиентов."""
        self.jobs, self.mappings, self.max_upload_bytes = jobs, mappings, max_upload_bytes

    @timed("specification_submit")
    async def submit(
        self, owner: str, key: str, content: bytes, filename: str, pages: str, project_code: str
    ) -> dict:
        """Сохраняет ограниченный PDF с параметрами; повтор того же ключа не создаёт дубль.

        Структуру и номера страниц проверяет Extracting после асинхронной передачи.
        Ошибка в PDF завершает задание явно, без потери принятого запроса при restart.
        """
        if not content or len(content) > self.max_upload_bytes:
            raise MapperError("upload_limit", "Превышен размер PDF или файл пуст", 413)
        if not content.startswith(b"%PDF-"):
            raise MapperError("invalid_pdf", "Ожидается файл PDF", 422)
        if not pages.strip() or len(pages) > 2000 or not re.fullmatch(r"[0-9,\s-]+", pages):
            raise MapperError("invalid_pages", "Укажите страницы PDF, например 1-5, 8", 422)
        project_code = project_code.strip()
        if not project_code or len(project_code) > 200 or any(ord(c) < 32 for c in project_code):
            raise MapperError("invalid_project_code", "Укажите шифр проекта до 200 символов", 422)
        filename = re.split(r"[/\\]", filename)[-1]
        if not filename or len(filename) > 180 or any(ord(c) < 32 for c in filename):
            raise MapperError("invalid_filename", "Некорректное имя PDF", 422)
        payload = {
            "specification": {
                "sha256": hashlib.sha256(content).hexdigest(),
                "filename": filename,
                "pages": pages.strip(),
                "project_code": project_code,
            }
        }
        return await self.jobs.create(owner, key, payload, "mapping", pdf=content)

    async def get(self, owner: str, job_id: str) -> dict:
        """Проверяет владельца, TTL и принадлежность задания процессу PDF."""
        job = await self.jobs.get(job_id, owner)
        if not job["data"].get("specification"):
            raise MapperError("not_found", "Спецификация не найдена", 404)
        return job

    async def rows(
        self, owner: str, job_id: str, q: str, status: str, offset: int, limit: int
    ) -> dict:
        """Возвращает страницу готовых строк без внутренних ID и маршрутов сервисов."""
        await self.get(owner, job_id)
        job = await self.mappings.result(owner, job_id)
        page = result_page(job, q, status, offset, limit)
        return {
            **{k: v for k, v in page.items() if k not in {"source_job_id", "kind", "stage"}},
            **specification_status(job),
        }


def specification_status(job: dict) -> dict:
    """Отображает технические этапы в состояния единого пользовательского процесса."""
    result = public_status(job)
    for field in ("source_job_id", "kind", "stage", "links"):
        result.pop(field)
    state = job["state"]
    if state in {"complete", "ready_for_review"}:
        state = "awaiting_review"
    elif state not in {"queued", "failed", "approved", "cancelling", "cancelled"}:
        state = "matching" if job["data"].get("source") else "extracting"
    root = f"/api/v1/specifications/{job['id']}"
    return {
        **result,
        "status": state,
        "links": {
            "status": root,
            "rows": root + "/rows",
            "result": root + "/result.json",
            "xlsx": root + "/result.xlsx",
            "approve": root + "/approve",
        },
    }


def specification_export(snapshot: dict) -> dict:
    """Убирает из JSON экспорта идентификатор внутреннего задания извлечения."""
    result = {key: value for key, value in snapshot.items() if key != "source_job_id"}
    if result.get("status") in {"complete", "ready_for_review"}:
        result["status"] = "awaiting_review"
    if "approval" in result:
        result["approval"] = {k: v for k, v in result["approval"].items() if k != "owner"}
    return result
