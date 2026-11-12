# src/infrastructure/json_catalog.py
"""Ограниченный потоковый parser полного JSON 1С без приведения кодов и артикулов."""

from __future__ import annotations

import asyncio
import json
import re
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from src.domain.models import CatalogItem, MapperError, ParsedCatalog, active_catalog

FIELDS = {
    "guid",
    "code",
    "name",
    "full_name",
    "unit",
    "article",
    "brand",
    "manufacturer",
    "deleted",
}


def unique_object(pairs):
    """Не допускает скрытой подмены значения повторным JSON-ключом."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторный JSON-ключ")
        result[key] = value
    return result


def parse_item(row: object) -> CatalogItem:
    """Проверяет согласованный тип карточки; внешний GUID никогда не заменяется кодом."""
    if not isinstance(row, dict) or set(row) != FIELDS:
        raise MapperError("catalog_schema", "Неполный или неизвестный набор полей карточки 1С")
    if type(row["deleted"]) is not bool or any(
        not isinstance(row[key], str) or len(row[key]) > (128 if key == "code" else 32000)
        for key in FIELDS - {"deleted"}
    ):
        raise MapperError(
            "catalog_schema", "Карточка требует строковые реквизиты и boolean deleted"
        )
    if not row["code"].strip() or not (row["full_name"] or row["name"]).strip():
        raise MapperError("catalog_schema", "Не заполнен код или наименование карточки")
    try:
        guid = str(UUID(row["guid"]))
        if UUID(guid).int == 0:
            raise ValueError("Пустая ссылка")
    except ValueError as exc:
        raise MapperError("catalog_guid", "Некорректный настоящий GUID 1С") from exc
    return CatalogItem(
        guid=guid,
        onec_guid=guid,
        code=row["code"],
        name=row["full_name"] or row["name"],
        short_name=row["name"],
        full_name=row["full_name"],
        unit=row["unit"],
        article=row["article"],
        brand=row["brand"],
        manufacturer=row["manufacturer"],
        active=not row["deleted"],
    )


def stream_items(path: Path, max_rows: int):
    """Читает только {items:[...]} с ограниченным буфером одной карточки и строгим EOF."""
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    with path.open(encoding="utf-8-sig") as stream:
        buffer, eof = "", False

        def more():
            """Догружает ограниченную пачку без загрузки всего каталога в память."""
            nonlocal buffer, eof
            chunk = stream.read(65536)
            eof = not chunk
            buffer += chunk
            if len(buffer) > 1000000:
                raise ValueError("Слишком большая карточка JSON")

        more()
        while not (match := re.match(r'\s*\{\s*"items"\s*:\s*\[', buffer)):
            if eof or len(buffer) > 1024:
                raise ValueError("Ожидается объект с единственным массивом items")
            more()
        buffer = buffer[match.end() :]
        count, separator = 0, False
        while True:
            buffer = buffer.lstrip()
            while not buffer and not eof:
                more()
                buffer = buffer.lstrip()
            if buffer.startswith("]"):
                buffer = buffer[1:]
                break
            if separator:
                if not buffer.startswith(","):
                    raise ValueError("Отсутствует разделитель карточек")
                buffer = buffer[1:].lstrip()
                while not buffer and not eof:
                    more()
                    buffer = buffer.lstrip()
            while True:
                try:
                    row, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if eof:
                        raise
                    more()
            count += 1
            if count > max_rows:
                raise ValueError("Превышено количество карточек")
            yield row
            buffer, separator = buffer[end:], True
        while not eof:
            more()
        if buffer.strip() != "}":
            raise ValueError("Лишние поля или неполный JSON")


class JsonCatalogSource:
    """Проверяет весь файл до staging и сохраняет неизменённые метаданные всех карточек."""

    def __init__(
        self, path: Path, max_bytes: int, max_rows: int, duplicate_policy="quarantine", full=True
    ):
        """Принимает private spool вместо пользовательского пути или сетевого URL."""
        self.path, self.max_bytes, self.max_rows, self.duplicate_policy = (
            path,
            max_bytes,
            max_rows,
            duplicate_policy,
        )
        self.full = full

    async def read(self) -> ParsedCatalog:
        """Выносит ограниченный parser из event loop."""
        return await asyncio.to_thread(self._read)

    def _read(self) -> ParsedCatalog:
        """Проверяет глубину до JSON decoder, UUID, код-конфликты и непустую полную выгрузку."""
        digest, depth, quoted, escape = sha256(), 0, False, False
        try:
            if self.path.stat().st_size > self.max_bytes:
                raise ValueError("Превышен объём каталога")
            with self.path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
                    for char in chunk:
                        if quoted:
                            if escape:
                                escape = False
                            elif char == 92:
                                escape = True
                            elif char == 34:
                                quoted = False
                        elif char == 34:
                            quoted = True
                        elif char in (91, 123):
                            depth += 1
                            if depth > 16:
                                raise ValueError("Превышена глубина JSON")
                        elif char in (93, 125):
                            depth -= 1
            records, ids = [], set()
            for row in stream_items(self.path, self.max_rows):
                item = parse_item(row)
                if item.onec_guid in ids:
                    raise MapperError("duplicate_guid", "JSON содержит повторный настоящий GUID 1С")
                ids.add(item.onec_guid)
                records.append(item)
            items, warnings, stats = active_catalog(records, self.duplicate_policy)
            if self.full and (not records or not items):
                raise MapperError(
                    "empty_catalog", "Полный JSON не содержит разрешённых активных позиций"
                )
            return ParsedCatalog(
                items, digest.hexdigest(), warnings, stats, records, {"full_json": self.full}
            )
        except MapperError:
            raise
        except (ValueError, OSError, RecursionError, UnicodeError) as exc:
            raise MapperError(
                "invalid_catalog_json", "Повреждённый, неполный или слишком большой JSON 1С"
            ) from exc
