# tests/test_xml.py
"""XML integration/regression: реальная схема, карантин, XXE, кодировки и устойчивые UUID."""

from __future__ import annotations

import pytest

from src.domain.models import MapperError
from src.infrastructure.xml_catalog import XmlCatalogSource


def xml_row(code="01", name="Клапан", deleted="Нет", guid=""):
    """Создаёт минимальную позицию фактического XML-формата без придуманной структуры 1С."""
    return (
        f'<Позиция код="{code}"><Наименование>{name}</Наименование>'
        "<ЕдиницаИзмерения>шт</ЕдиницаИзмерения>"
        f"<ПометкаУдаления>{deleted}</ПометкаУдаления>{guid}</Позиция>"
    )


def write_xml(tmp_path, content):
    """Записывает UTF-8 fixture с действительным корневым элементом выгрузки."""
    (tmp_path / "catalog.xml").write_text(
        "<СправочникНоменклатура>" + content + "</СправочникНоменклатура>", encoding="utf-8"
    )
    return XmlCatalogSource(tmp_path, 100000, "quarantine")


async def test_real_schema_stable_identity_and_deletions(tmp_path):
    """Исключает удалённые позиции и не выдаёт внутренний UUID за ссылку GUID 1С."""
    source = write_xml(tmp_path, xml_row() + xml_row("02", deleted="Да"))
    first, second = await source.read(), await source.read()
    assert len(first.items) == 1
    assert first.items[0].guid == second.items[0].guid
    assert first.items[0].onec_guid is None
    assert first.warnings


async def test_all_conflicting_code_rows_are_quarantined(tmp_path):
    """Регрессия фактической выгрузки: ни первая, ни последняя конфликтующая
    запись не выбирается.
    """
    source = write_xml(tmp_path, xml_row("01", "Первый") + xml_row("01", "Второй") + xml_row("02"))
    parsed = await source.read()
    assert [item.code for item in parsed.items] == ["02"]
    assert any("Карантин" in warning and "01" in warning for warning in parsed.warnings)


async def test_guid_duplicates_fail_even_with_quarantine(tmp_path):
    """Настоящий дубликат GUID — ошибка источника, которую нельзя скрыть политикой кодов."""
    guid = "<GUID>5e2cfd2a-51e3-44ba-88bd-914a93aa4a1c</GUID>"
    source = write_xml(tmp_path, xml_row("01", guid=guid) + xml_row("02", guid=guid))
    with pytest.raises(MapperError) as error:
        await source.read()
    assert error.value.code == "duplicate_guid"


@pytest.mark.parametrize(
    "content",
    [
        '<!DOCTYPE x [<!ENTITY bad SYSTEM "file:///etc/passwd">]><СправочникНоменклатура/>',
        "<СправочникНоменклатура><Позиция>",
        "<ДругаяСхема/>",
    ],
)
async def test_malformed_or_external_entities_are_rejected(tmp_path, content):
    """Повреждённый XML, неизвестная схема и XXE не становятся успешным пустым справочником."""
    (tmp_path / "catalog.xml").write_text(content, encoding="utf-8")
    with pytest.raises(MapperError):
        await XmlCatalogSource(tmp_path, 100000, "quarantine").read()


async def test_empty_size_limit_and_missing_catalog(tmp_path):
    """Превышение размера, отсутствие XML и пустой активный каталог явно диагностируются."""
    with pytest.raises(MapperError):
        await XmlCatalogSource(tmp_path, 1, "quarantine").read()
    source = write_xml(tmp_path, xml_row(deleted="Да"))
    with pytest.raises(MapperError) as error:
        await source.read()
    assert error.value.code == "empty_catalog"
