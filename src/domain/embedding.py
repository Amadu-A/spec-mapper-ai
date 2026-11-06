# src/domain/embedding.py
"""Проверка совместимости модели с допуском численной погрешности GPU."""

from __future__ import annotations

import math


def compatible_embedding(expected: dict, actual: dict) -> bool:
    """Сверяет metadata и контрольный вектор, сохраняя строгую проверку старого формата."""
    if not expected.get("metadata_fingerprint") or not actual.get("metadata_fingerprint"):
        return bool(expected.get("fingerprint")) and expected["fingerprint"] == actual.get(
            "fingerprint"
        )
    if expected["metadata_fingerprint"] != actual["metadata_fingerprint"]:
        return False
    left, right = expected.get("probe_vector", []), actual.get("probe_vector", [])
    if not left or len(left) != len(right):
        return False
    if any(not math.isfinite(value) for value in [*left, *right]):
        return False
    norm = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    if not norm:
        return False
    cosine = sum(a * b for a, b in zip(left, right, strict=True)) / norm
    return cosine >= 1 - 1e-6 and max(
        abs(a - b) for a, b in zip(left, right, strict=True)
    ) <= 1e-4 * max(1.0, *(abs(v) for v in left))
