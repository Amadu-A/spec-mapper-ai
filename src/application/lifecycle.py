# src/application/lifecycle.py
"""Общие owner-safe lifecycle/list/cancel use-cases поверх существующего агрегата jobs."""

from __future__ import annotations

from src.domain.models import MapperError


class JobLifecycle:
    """Не создаёт upstream polling на вкладку и не дублирует profile-specific matching."""

    def __init__(self, jobs) -> None:
        """Получает общий репозиторий и его durable metadata/journal."""
        self.jobs = jobs

    async def get(self, owner: str, job_id: str, family: str) -> dict:
        """Применяет профильную проверку после owner/TTL, сохраняя legacy mapping совместимость."""
        value = await self.jobs.lifecycle_view(owner, job_id)
        expected = "invoice" if family == "invoices" else "specification"
        if value["profile"] != expected or (
            family == "specifications" and value["api_root"] != "/api/v1/specifications"
        ):
            raise MapperError("not_found", "Задание этого профиля не найдено", 404)
        return value

    async def list(self, owner: str, family: str, offset: int, limit: int, state: str) -> dict:
        """Рабочее пространство спецификаций также восстанавливает прежние JSON/UUID входы."""
        return await self.jobs.history_page(
            owner, "invoice" if family == "invoices" else "specification", offset, limit, state
        )

    async def cancel(self, owner: str, job_id: str, family: str) -> dict:
        """Профиль/owner проверяются до общей атомарной отмены, включая отказ от draft."""
        await self.get(owner, job_id, family)
        await self.jobs.cancel(owner, job_id)
        return await self.get(owner, job_id, family)

    async def events(self, owner: str, job_id: str, family: str, after: int) -> list[dict]:
        """Поток читает только журнал Mapper; не обращается к модели или upstream API."""
        await self.get(owner, job_id, family)
        return await self.jobs.lifecycle_events(owner, job_id, after)
