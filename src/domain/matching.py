# src/domain/matching.py
"""Нормализация и объяснимое ранжирование без обучения и без изменения исходного JSON."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from src.domain.models import Candidate, CatalogItem

UNIT_ALIASES = {
    "шт": "шт",
    "штука": "шт",
    "штук": "шт",
    "pcs": "шт",
    "м": "м",
    "погм": "м",
    "мп": "м",
    "м2": "м2",
    "м²": "м2",
    "м3": "м3",
    "м³": "м3",
    "кг": "кг",
    "т": "т",
    "компл": "компл",
    "комплект": "компл",
    "л": "л",
}
PARAMETERS = {
    "DN": r"(?:dn|ду|дн)\s*=?\s*(\d+(?:[.,]\d+)?)",
    "PN": r"(?:pn|ру|рн)\s*=?\s*(\d+(?:[.,]\d+)?)",
    "кВт": r"(\d+(?:[.,]\d+)?)\s*(?:квт|kw)",
    "бар": r"(\d+(?:[.,]\d+)?)\s*(?:бар|bar)",
    "МПа": r"(\d+(?:[.,]\d+)?)\s*(?:мпа|mpa)",
    "расход": r"(\d+(?:[.,]\d+)?)\s*м[³3]/ч",
    "напор": r"h\s*=?\s*(\d+(?:[.,]\d+)?)\s*м",
    "температура": r"([+-]?\d+(?:[.,]\d+)?)\s*°\s*[cс]",
}
RANGE_NUMBER = r"(\d+(?:[.,]\d+)?)\s*[-–—…]\s*(\d+(?:[.,]\d+)?)"
RANGE_PARAMETERS = {
    "DN": rf"(?:dn|ду|дн)\s*=?\s*{RANGE_NUMBER}",
    "PN": rf"(?:pn|ру|рн)\s*=?\s*{RANGE_NUMBER}",
    "кВт": rf"{RANGE_NUMBER}\s*(?:квт|kw)",
    "бар": rf"{RANGE_NUMBER}\s*(?:бар|bar)",
    "МПа": rf"{RANGE_NUMBER}\s*(?:мпа|mpa)",
    "расход": rf"{RANGE_NUMBER}\s*м[³3]/ч",
    "напор": rf"h\s*=?\s*{RANGE_NUMBER}\s*м",
    "температура": (
        r"([+-]?\d+(?:[.,]\d+)?)\s*(?:[-–—…]|\.{2,3})\s*"
        r"([+-]?\d+(?:[.,]\d+)?)\s*°\s*[cс]"
    ),
}


def normalize(text: str) -> str:
    """Убирает только различия регистра и пробелов, сохраняя марки и размерности."""
    return " ".join(unicodedata.normalize("NFC", text).lower().replace("ё", "е").split())


def unit_key(text: str) -> str:
    """Приводит синонимы единиц без неявного пересчёта метров, массы и комплектов."""
    value = normalize(text).replace(".", "").replace(" ", "")
    return UNIT_ALIASES.get(value, value)


def parameters(text: str) -> dict[str, set[float]]:
    """Выделяет подтверждённые числа и границы диапазонов технических параметров."""
    text = normalize(text)
    result = {}
    for key, pattern in PARAMETERS.items():
        values = set()
        for bounds in re.findall(RANGE_PARAMETERS[key], text):
            values.update(float(value.replace(",", ".")) for value in bounds)
        remaining = re.sub(RANGE_PARAMETERS[key], " ", text)
        values.update(float(value.replace(",", ".")) for value in re.findall(pattern, remaining))
        if values:
            result[key] = values
    return result


def rerank(source: dict, hits: list[tuple[CatalogItem, float]]) -> list[Candidate]:
    """Штрафует конфликтующие единицы, параметры и марки, сохраняя исходный cosine."""
    query = normalize(source["Caption"])
    expected = parameters(query)
    article = re.search(r"(?:арт(?:икул)?\.?|article)\s*[:=№]?\s*([a-zа-я0-9][\w./-]*)", query)
    model = re.search(r"(?:модель|model)\s*[:=]?\s*([a-zа-я0-9][\w./-]*)", query)
    known_brands = {
        normalize(value) for item, _ in hits for value in (item.brand, item.manufacturer) if value
    }
    requested_brands = {brand for brand in known_brands if brand in query}
    latin_words = set(re.findall(r"\b[a-z]{3,}\b", query))
    ranked = []
    for item, score in hits:
        title = normalize(item.name)
        actual = parameters(title)
        conflicts, evidence = [], []
        if article and item.article and article.group(1) != normalize(item.article):
            conflicts.append("Различается артикул")
        elif article and not item.article:
            evidence.append("Артикул не подтверждён")
        if model and not re.search(r"(?<!\w)" + re.escape(model.group(1)) + r"(?!\w)", title):
            evidence.append("Модель не подтверждена")
        item_text = normalize(" ".join((item.name, item.article, item.brand, item.manufacturer)))
        if requested_brands and not all(brand in item_text for brand in requested_brands):
            conflicts.append("Различается марка или производитель")
        if not latin_words.issubset(set(re.findall(r"\b[a-z]{3,}\b", item_text))):
            evidence.append("Марка или производитель не подтверждены")
        source_unit, item_unit = unit_key(source["Units"]), unit_key(item.unit)
        if source_unit and item_unit and source_unit != item_unit:
            conflicts.append("Несовместимые единицы измерения")
        if not source_unit or not item_unit:
            evidence.append("Единица измерения не подтверждена")
        for key, values in expected.items():
            if key not in actual:
                evidence.append(f"Не подтверждён параметр {key}")
            elif values != actual[key]:
                conflicts.append(f"Различается параметр {key}")
        query_marks = set(re.findall(r"\b[a-zа-я]+[\w./-]*\d[\w./-]*", query))
        title_marks = set(re.findall(r"\b[a-zа-я]+[\w./-]*\d[\w./-]*", title))
        if query_marks and not query_marks.issubset(title_marks):
            evidence.append("Марка или артикул не подтверждены полностью")
        for key in (item.brand, item.manufacturer, item.article):
            if key and normalize(key) in query:
                evidence.append("Подтверждена марка, производитель или артикул")
        lexical = SequenceMatcher(None, query, title).ratio()
        rank = 0.8 * max(-1.0, min(1.0, score)) + 0.2 * lexical - 0.35 * len(conflicts)
        ranked.append(Candidate(item, score, round(rank, 6), tuple(conflicts), tuple(evidence)))
    return sorted(ranked, key=lambda c: (-c.rank, c.item.code, c.item.guid))


def choose(candidates: list[Candidate], threshold: float, margin: float) -> dict:
    """Завершает подбор даже без кандидатов; непроверенные признаки требуют ревью."""
    if not candidates:
        return {"selection": None, "status": "not_found", "confirmed": False}
    best = candidates[0]
    gap = best.rank - candidates[1].rank if len(candidates) > 1 else 1.0
    uncertain = any("не подтвержд" in e.lower() for e in best.evidence)
    automatic = not best.conflicts and not uncertain and best.rank >= threshold and gap >= margin
    return {
        "selection": best.item.to_dict() if not best.conflicts else None,
        "status": "matched" if automatic else "review",
        "confirmed": False,
    }
