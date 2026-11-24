# src/application/invoices/ports.py
"""Порты ввода пакета и серверного invoice API, без HTTP/ORM в use-case."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Upload:
    """Один ограничиваемый поток входного файла, переданный transport-адаптером."""

    filename: str
    media_type: str
    chunks: AsyncIterator[bytes]


class UploadInspector(Protocol):
    """Структурная проверка PDF/изображения без OCR и собственного preview renderer."""

    def inspect(self, content: bytes, media_type: str) -> tuple[str, int]:
        """Возвращает фактический MIME и число физических страниц либо предметную ошибку."""
        ...


class InvoiceExtraction(Protocol):
    """Server-to-server профиль Extracting с приватным upstream ID и bounded ответами."""

    async def submit(self, metadata: dict, files: list[bytes], key: str) -> str:
        """Идемпотентно передаёт пакет для долговечной обработки 8083."""
        ...

    async def fetch(self, job_id: str, progress) -> dict:
        """Дожидается только полного invoice_extraction.v1, сохраняя предупреждение финала."""
        ...

    async def preview(self, job_id: str, file_index: int, page: int, width: int) -> bytes:
        """Получает ограниченный PNG через известный маршрут с серверным API-ключом."""
        ...
