# alembic/env.py
"""Async миграции собственной PostgreSQL через тот же Pydantic Settings без вывода DSN."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from src.config import Settings
from src.infrastructure.database import Base


def migrate(connection) -> None:
    """Применяет ревизии в одной контролируемой Alembic транзакции."""
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    """Получает DSN из Pydantic, не передавая пароль через CLI или config interpolation."""
    engine = create_async_engine(Settings().database_url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
