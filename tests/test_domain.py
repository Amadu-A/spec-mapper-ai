# tests/test_domain.py
"""Unit/regression тесты неизменного JSON и технических запретов автоматического выбора."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain.matching import choose, normalize, rerank, unit_key
from src.domain.models import CatalogItem, MapperError, validate_source


def test_source_is_verbatim(source):
    """Проверяет сохранение дублей, пробелов, дробного количества и шифра проекта."""
    assert validate_source(source) is source
    assert len(source["items"]) == 2
    assert source["items"][0]["Quantity"] == " 2,50 "


@pytest.mark.parametrize("mutation", ["numeric", "missing", "extra", "root"])
def test_invalid_contract_is_rejected(source, mutation):
    """Числа, лишние или недостающие поля не исправляются автоматически."""
    if mutation == "numeric":
        source["items"][0]["Quantity"] = 2
    elif mutation == "missing":
        source["items"][0].pop("Pages")
    elif mutation == "extra":
        source["items"][0]["code"] = "bad"
    else:
        source["partial"] = {}
    with pytest.raises(MapperError, match="исходных|строками|единственным"):
        validate_source(source)


@pytest.mark.parametrize(
    "name,unit", [("Клапан DN40 PN16", "шт"), ("Клапан DN50 PN25", "шт"), ("Клапан DN50 PN16", "м")]
)
def test_high_cosine_cannot_hide_conflict(name, unit):
    """Даже cosine=1 не допускает автоматическое соответствие при конфликте DN, PN или единицы."""
    row = {"Caption": "Клапан DN50 PN16", "Units": "шт"}
    candidates = rerank(row, [(CatalogItem("id", "code", name, unit), 1.0)])
    assert candidates[0].conflicts
    result = choose(candidates, 0.88, 0.05)
    assert result["status"] == "review"
    assert result["selection"] is None


def test_missing_parameter_and_ambiguity_require_review():
    """Отсутствующий PN и почти одинаковые кандидаты никогда не объявляются надёжным подбором."""
    row = {"Caption": "Клапан DN50 PN16", "Units": "шт"}
    item = CatalogItem("id", "1", "Клапан DN50", "шт")
    assert choose(rerank(row, [(item, 1)]), 0.5, 0)["status"] == "review"
    exact = replace(item, name=row["Caption"])
    assert (
        choose(rerank(row, [(exact, 1), (replace(exact, guid="id2"), 0.99)]), 0.88, 0.05)["status"]
        == "review"
    )


def test_empty_hits_finish_and_normalization_preserves_technical_data():
    """Пустой Top-K — законченный результат; нормализация не стирает технические характеристики."""
    assert choose([], 0.88, 0.05)["status"] == "not_found"
    assert normalize("  DN50  PN16 / 12,5кВт ") == "dn50 pn16 / 12,5квт"
    assert unit_key("шт.") == unit_key("штука")


@pytest.mark.parametrize(
    "query,title",
    [
        ("Манометр 0–10 бар", "Манометр 5–10 бар"),
        ("Теплообменник 300–400 кВт", "Теплообменник 350–400 кВт"),
        ("Привод 12,5 kW", "Привод 15 кВт"),
    ],
)
def test_ranges_and_latin_power_cannot_hide_conflict(query, title):
    """Разные нижние границы и обозначение kW не обходят технические запреты выбора."""
    candidates = rerank(
        {"Caption": query, "Units": "шт"},
        [
            (CatalogItem("id", "code", title, "шт"), 1.0),
        ],
    )
    assert candidates[0].conflicts
    assert choose(candidates, 0.88, 0.05)["status"] == "review"
