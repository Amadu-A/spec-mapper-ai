# tests/conftest.py
"""Offline fixtures: настоящий SQLAlchemy adapter в SQLite и явные fake внешних сервисов."""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest

from src.application.catalog import PruneCatalogUseCase, SynchronizeCatalogUseCase
from src.application.processing import JobProcessor
from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import CatalogItem, ParsedCatalog
from src.infrastructure.database import Base


@pytest.fixture(autouse=True)
def isolate_application_environment(request, monkeypatch):
    """Offline tests не наследуют реальные настройки оператора; live tests сохраняют окружение."""
    if request.node.get_closest_marker("live"):
        return
    names = {name.casefold() for name in Settings.model_fields}
    for name in tuple(os.environ):
        if name.casefold() in names:
            monkeypatch.delenv(name)


@pytest.fixture
def settings():
    """Задаёт только фиктивные credentials и короткий polling без загрузки реального .env."""
    return Settings(
        _env_file=None,
        api_key="a" * 32,
        catalog_admin_key="b" * 32,
        session_secret="c" * 32,
        database_password="d" * 32,
        rabbitmq_password="e" * 32,
        extracting_api_key="f" * 32,
        poll_seconds=0.05,
    )


@pytest.fixture
def source():
    """Сохраняет пробелы, количество строкой и повторяющиеся строки для регрессий контракта."""
    row = {
        "Caption": "Клапан балансировочный DN50 PN16",
        "Units": "шт",
        "Quantity": " 2,50 ",
        "UserComment1": "Проект 01",
        "Pages": "4–5",
    }
    return {"items": [row.copy(), row.copy()]}


@dataclass
class FakeEmbeddings:
    """Подменяет только сетевую векторизацию; никогда не выдаётся за live модель."""

    fingerprint: str = "fp1"
    calls: int = 0

    async def identity(self):
        """Возвращает управляемую identity для проверки несовместимости моделей."""
        return {"fingerprint": self.fingerprint, "dimension": 2}

    async def embed(self, texts):
        """Считает реально запрошенные тексты для инкрементальных regression tests."""
        self.calls += len(texts)
        return [[1.0, 0.0] for _ in texts]


class FakeIndex:
    """Проверяет staging/alias протокол in-memory без утверждений о live Qdrant."""

    def __init__(self):
        """Хранит несколько независимых коллекций, чтобы проверить отсутствие delete активной."""
        self.collections, self.alias, self.bad_count = {}, None, False

    async def create(self, collection, dimension):
        """Создаёт новую staging коллекцию и запрещает overwrite существующей."""
        assert collection not in self.collections
        self.collections[collection] = {}

    async def exists(self, collection):
        """Проверяет наличие staging для offline проверки восстановления импорта."""
        return collection in self.collections

    async def clone(self, source, target):
        """Копирует готовый индекс без новых upsert для проверки incremental контракта."""
        assert target not in self.collections
        self.collections[target] = dict(self.collections[source])

    async def delete_points(self, collection, ids):
        """Удаляет только устаревшие точки staging снимка."""
        for guid in ids:
            self.collections[collection].pop(guid, None)

    async def put(self, collection, points):
        """Применяет idempotent upsert UUID-точек ограниченной пачки."""
        self.collections[collection].update({p["id"]: p["vector"] for p in points})

    async def count(self, collection):
        """Имитирует отдельную проверку count и её преднамеренный отказ."""
        return -1 if self.bad_count else len(self.collections[collection])

    async def remove(self, collection):
        """Удаляет только переданную устаревшую test коллекцию, сохраняя активный alias."""
        assert collection != self.alias
        self.collections.pop(collection, None)

    async def switch(self, collection):
        """Переключает alias, возвращая прежнюю коллекцию для rollback."""
        old, self.alias = self.alias, collection
        return old

    async def restore(self, collection):
        """Восстанавливает старый alias при ошибке фиксации каталога."""
        self.alias = collection

    async def search(self, collection, vectors, limit):
        """Возвращает отдельный список кандидатов на каждую входную строку."""
        return [
            [(guid, 0.99) for guid in list(self.collections[collection])[:limit]] for _ in vectors
        ]


class FakeSource:
    """Предоставляет изменяемый проверенный снимок вместо файлового адаптера."""

    def __init__(self, items):
        """Сохраняет исходную версию для проверки повторных синхронизаций."""
        self.items, self.source_hash = items, "xml1"

    async def read(self):
        """Возвращает независимые позиции без сетевого или файлового доступа."""
        return ParsedCatalog(self.items, self.source_hash)


class FakeExtraction:
    """Передаёт окончательный payload, отмечая число успешных загрузок source snapshot."""

    def __init__(self, payload):
        """Задаёт контракт upstream для test pipeline."""
        self.payload, self.calls = payload, 0

    async def fetch(self, job_id, progress):
        """Записывает этап получения окончательного JSON, не имитируя partial результат."""
        self.calls += 1
        await progress("fetching")
        return self.payload


@pytest.fixture
async def system(tmp_path, settings, source):
    """Собирает реальные репозитории и fake внешние порты для API/application tests."""
    container = build_container(
        settings, "sqlite+aiosqlite:///" + (tmp_path / "test.db").as_posix()
    )
    async with container.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    item = CatalogItem(str(uuid4()), "00-00001", source["items"][0]["Caption"], "шт")
    embeddings, index, catalog_source = FakeEmbeddings(), FakeIndex(), FakeSource([item])
    sync = SynchronizeCatalogUseCase(catalog_source, container.catalog, embeddings, index, 2)

    async def progress(stage, **details):
        """Принимает этапы standalone test импорта без HTTP и durable job."""
        return None

    await sync.execute(progress)
    extraction = FakeExtraction(source)
    container.processor = JobProcessor(
        container.jobs,
        container.catalog,
        extraction,
        embeddings,
        index,
        sync,
        settings,
        catalog_sources=container.catalog_sources,
        catalog_management=container.sync,
    )
    container.catalog_sources.local = catalog_source
    container.embeddings, container.index = embeddings, index
    container.pruner = PruneCatalogUseCase(container.catalog, index)
    yield container, item, sync, catalog_source, extraction
    await container.close()


def pytest_addoption(parser):
    """Позволяет явно передать UUID готового upstream задания только для live contract проверки."""
    parser.addoption("--source-job-id", default=None, help="UUID API-задания spec-extracting-ai")
