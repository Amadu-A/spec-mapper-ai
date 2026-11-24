# src/infrastructure/invoices/uploads.py
"""Проверяет структуру ограниченных PDF/JPEG/PNG до атомарного приёма пакета."""

from io import BytesIO

import pymupdf
from PIL import Image, UnidentifiedImageError

from src.domain.models import MapperError


class InvoiceUploadInspector:
    """Считает страницы и проверяет MIME/целостность; OCR/preview выполняет только 8083."""

    def inspect(self, content: bytes, media_type: str) -> tuple[str, int]:
        """Отклоняет поддельный MIME, повреждённый/защищённый документ и image bomb."""
        actual = (
            "application/pdf"
            if content.startswith(b"%PDF-")
            else "image/png"
            if content.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if content.startswith(b"\xff\xd8\xff")
            else None
        )
        if actual is None or media_type not in {actual, "application/octet-stream"}:
            raise MapperError(
                "unsupported_media_type", "Ожидается корректный PDF/JPEG/PNG MIME", 415
            )
        try:
            if actual == "application/pdf":
                with pymupdf.open(stream=content, filetype="pdf") as document:
                    if document.needs_pass or not 1 <= document.page_count <= 500:
                        raise ValueError("Защищённый/пустой PDF либо превышен предел страниц")
                    return actual, document.page_count
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Ожидается одно изображение")
                image.verify()
            return actual, 1
        except (
            RuntimeError,
            ValueError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as exc:
            raise MapperError(
                "invalid_invoice_file", "Файл повреждён, защищён или превышает лимиты", 422
            ) from exc
