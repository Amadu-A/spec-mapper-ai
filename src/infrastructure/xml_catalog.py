# src/infrastructure/xml_catalog.py
"""Потоковый безопасный импорт фактической XML-выгрузки СправочникНоменклатура."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from src.domain.models import CatalogItem, MapperError, ParsedCatalog


class XmlCatalogSource:
    """Читает только разрешённые XML в data; неоднозначные коды помещает в карантин."""

    def __init__(self, directory: Path, max_bytes: int, duplicate_policy: str) -> None:
        """Фиксирует каталог-границу, лимит суммарного объёма и принятую политику конфликтов."""
        self.directory, self.max_bytes, self.duplicate_policy = (
            directory,
            max_bytes,
            duplicate_policy,
        )

    async def read(self) -> ParsedCatalog:
        """Выносит блокирующий XML-парсинг в один ограниченный worker thread."""
        return await asyncio.to_thread(self._read)

    def _read(self) -> ParsedCatalog:
        """Проверяет структуру, UUID и дубликаты до предоставления снимка для индексации."""
        directory = self.directory.resolve()
        files = sorted(directory.glob("*.xml"))
        if not files or sum(p.stat().st_size for p in files) > self.max_bytes:
            raise MapperError("catalog_files", "XML отсутствуют или превышают допустимый объём")
        by_guid, code_ids, conflicts, digest = {}, {}, set(), sha256()
        without_guid = 0
        try:
            for path in files:
                if path.is_symlink() or path.resolve().parent != directory:
                    raise MapperError(
                        "catalog_path", "XML должен находиться непосредственно в data"
                    )
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                root = None
                for event, element in ElementTree.iterparse(
                    path,
                    events=("start", "end"),
                    forbid_dtd=True,
                    forbid_entities=True,
                    forbid_external=True,
                ):
                    if root is None:
                        root = element
                        if root.tag != "СправочникНоменклатура":
                            raise MapperError("catalog_schema", "Неизвестный корневой элемент XML")
                    if event != "end" or element.tag != "Позиция":
                        continue
                    values = {child.tag: child.text or "" for child in element}
                    if len(values) != len(element):
                        raise MapperError("catalog_schema", "Повторяющиеся поля позиции XML")
                    code = (element.get("код") or "").strip()
                    name = (
                        values.get("НаименованиеПолное") or values.get("Наименование", "")
                    ).strip()
                    deleted = values.get("ПометкаУдаления", "")
                    if not code or not name or deleted not in {"Да", "Нет"}:
                        raise MapperError(
                            "catalog_schema", "Не заполнен код, имя или пометка удаления"
                        )
                    if any(len(value) > 32000 for value in values.values()) or len(code) > 128:
                        raise MapperError("catalog_schema", "Превышена длина поля XML")
                    external = values.get("GUID") or values.get("Ссылка") or element.get("guid")
                    try:
                        real_guid = str(UUID(external)) if external else None
                    except ValueError as exc:
                        raise MapperError("catalog_guid", "Некорректная ссылка GUID 1С") from exc
                    guid = real_guid or str(uuid5(NAMESPACE_URL, f"spec-mapper:code:{code}"))
                    if not real_guid:
                        without_guid += 1
                    if guid in by_guid and real_guid:
                        raise MapperError("duplicate_guid", "XML содержит повторный GUID 1С")
                    if code in code_ids and (not real_guid or code_ids[code] == guid):
                        conflicts.add(code)
                    code_ids[code] = guid
                    by_guid[guid] = CatalogItem(
                        guid=guid,
                        onec_guid=real_guid,
                        code=code,
                        name=name,
                        unit=values.get("ЕдиницаИзмерения", "").strip(),
                        article=values.get("Артикул", "").strip(),
                        brand=values.get("Марка", "").strip(),
                        manufacturer=values.get("Производитель", "").strip(),
                        group=values.get("Группа", "").strip(),
                        active=deleted == "Нет",
                    )
                    root.remove(element)
            if conflicts and self.duplicate_policy == "reject":
                raise MapperError("duplicate_code", "XML содержит конфликтующие коды номенклатуры")
        except (ElementTree.ParseError, DefusedXmlException, OSError) as exc:
            raise MapperError(
                "invalid_xml", "XML повреждён, недоступен или содержит запрещённые сущности"
            ) from exc
        warnings = []
        if without_guid:
            warnings.append(
                "GUID 1С отсутствует: внутренние UUID получены из кода; это не ссылки 1С"
            )
        if conflicts:
            warnings.append(
                "Карантин: исключены все записи с кодами " + ", ".join(sorted(conflicts))
            )
        items = [item for item in by_guid.values() if item.active and item.code not in conflicts]
        if not items:
            raise MapperError("empty_catalog", "XML не содержит разрешённых активных позиций")
        return ParsedCatalog(items, digest.hexdigest(), warnings)
