# tests/test_embedding_identity.py
"""Регрессия live-прогона: численный шум GPU не означает смену модели или новый индекс."""

from copy import deepcopy

import pytest

from src.domain.embedding import compatible_embedding


def identity(probe, fingerprint="reference", metadata="model-a"):
    """Создаёт контролируемый ответ порта embedding без обращения к реальной модели."""
    return {
        "fingerprint": fingerprint,
        "metadata_fingerprint": metadata,
        "probe_vector": probe,
        "dimension": len(probe),
    }


@pytest.mark.parametrize(
    "actual,compatible",
    [
        (identity([1.0, 1e-7], "other-numeric-hash"), True),
        (identity([0.8, 0.6]), False),
        (identity([1.0, 0.0], metadata="model-b"), False),
        (identity([0.0, 0.0]), False),
        (identity([float("nan"), 0.0]), False),
        (identity([1.0]), False),
    ],
)
def test_numeric_noise_is_allowed_but_model_drift_is_rejected(actual, compatible):
    """Допуск не разрешает другую revision, размерность, нулевой вектор или смену семантики."""
    assert compatible_embedding(identity([1.0, 0.0]), actual) is compatible


async def test_legacy_catalog_adopts_probe_and_keeps_index_under_numeric_noise(system, source):
    """Старый индекс получает проверенный reference, затем переживает шум без переиндексации."""
    container, _, sync, *_ = system
    before = await container.catalog.status()
    metadata = deepcopy(before)
    for key in ("ready", "version", "last_successful_sync", "last_error", "embedding_identity"):
        metadata.pop(key, None)
    await container.catalog.update_stage(before["version"], metadata)
    current = identity([1.0, 0.0], "fp1")

    async def actual_identity():
        """Возвращает изменяемый численный probe при неизменной модели."""
        return deepcopy(current)

    async def progress(stage, **details):
        """Принимает этапы настоящего use-case в offline-окружении."""
        return None

    container.embeddings.identity = actual_identity
    calls = container.embeddings.calls
    await sync.execute(progress)
    current.update(probe_vector=[1.0, 1e-7], fingerprint="noisy-hash")
    after = await sync.execute(progress)
    assert after["version"] == before["version"]
    assert after["fingerprint"] == "fp1"
    assert container.embeddings.calls == calls
    job = await container.mappings.import_document("owner", "numeric", source)
    await container.processor.process(job["id"])
    assert (await container.jobs.get(job["id"]))["state"] == "complete"
