# src/infrastructure/job_delivery.py
"""Короткие транзакции transport outbox; никакой публикации или AMQP под SQL lock."""

import logging
from random import uniform
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import defer

from src.domain.models import TERMINAL, MapperError
from src.infrastructure.database import JobRow


async def reserve(jobs) -> dict | None:
    """Восстанавливает только известные оборванные попытки, затем резервирует один publish."""
    now = jobs.clock()
    async with jobs.transaction() as session:
        interrupted = await session.scalars(
            select(JobRow).where(
                JobRow.state.not_in(TERMINAL),
                JobRow.expires_at > now,
                JobRow.delivery_state.in_(["publishing", "received"]),
            )
        )
        for row in interrupted:
            if row.delivery_state == "publishing" and (row.delivery_until or 0) <= now:
                row.delivery_state, row.delivery_token, row.delivery_until = "pending", None, None
                row.delivery_next_at = now
                row.delivery_error = "publisher_interrupted"
            elif row.delivery_state == "received" and (row.lease_until or 0) <= now:
                row.delivery_state = "pending"
                row.delivery_generation += 1
                row.delivery_recoveries += 1
                logging.getLogger(__name__).info(
                    "delivery_lease_recovery",
                    extra={"job_id": row.id, "generation": row.delivery_generation},
                )
                row.delivery_next_at = now
                row.delivery_token, row.delivery_until = None, None
                row.lease_token, row.lease_until = None, None
                row.delivery_published_at, row.delivery_received_at = None, None
                await jobs.event(session, row, "lease_recovered")
        await session.flush()
        row = await session.scalar(
            select(JobRow)
            .where(
                JobRow.state.not_in(TERMINAL),
                JobRow.expires_at > now,
                JobRow.delivery_state == "pending",
                JobRow.delivery_next_at <= now,
            )
            .order_by(JobRow.delivery_next_at, JobRow.created_at, JobRow.id)
            .limit(1)
        )
        if row is None:
            return None
        row.delivery_state, row.delivery_token = "publishing", str(uuid4())
        row.delivery_until = now + jobs.settings.delivery_reservation_seconds
        row.delivery_attempts += 1
        return {
            "job_id": row.id,
            "generation": row.delivery_generation,
            "token": row.delivery_token,
            "attempt": row.delivery_attempts,
        }


async def confirm(jobs, attempt: dict) -> bool:
    """Поздний confirm не может подтвердить новую generation или стереть durable receipt."""
    async with jobs.transaction() as session:
        row = await session.get(JobRow, attempt["job_id"])
        if not row or (row.delivery_generation, row.delivery_token) != (
            attempt["generation"],
            attempt["token"],
        ):
            return False
        if row.delivery_state == "publishing":
            row.delivery_state = "published"
        row.delivery_published_at = jobs.clock()
        row.dispatched_at = jobs.clock()
        row.delivery_until, row.delivery_error = None, None
        return True


async def failure(jobs, attempt: dict, code: str) -> bool:
    """Receipt сильнее потери confirm; только неполученная публикация получает backoff."""
    async with jobs.transaction() as session:
        row = await session.get(JobRow, attempt["job_id"])
        if (
            not row
            or row.delivery_state != "publishing"
            or (row.delivery_generation, row.delivery_token)
            != (attempt["generation"], attempt["token"])
        ):
            return False
        delay = min(60, 2 ** min(row.delivery_attempts, 6))
        row.delivery_state = "pending"
        row.delivery_token, row.delivery_until = None, None
        row.delivery_next_at = jobs.clock() + uniform(delay * 0.8, delay)
        row.delivery_error = code[:64]
        return True


async def reconcile(jobs, identifiers: list[str]) -> dict:
    """Только явные UUID оператора: bounded recovery после drain или доказанной утраты очереди."""
    if not 1 <= len(identifiers) <= 10 or len(set(identifiers)) != len(identifiers):
        raise MapperError("invalid_reconciliation", "Укажите от 1 до 10 разных UUID", 422)
    scheduled, unchanged = [], []
    async with jobs.transaction() as session:
        for identifier in identifiers:
            row = await session.get(JobRow, identifier)
            if (
                not row
                or row.state in TERMINAL
                or row.expires_at <= jobs.clock()
                or (row.lease_until or 0) > jobs.clock()
                or row.delivery_state not in {"legacy_unknown", "published"}
                or (row.delivery_published_at or 0) + 300 > jobs.clock()
            ):
                unchanged.append(identifier)
                continue
            row.delivery_generation += 1
            row.delivery_state, row.delivery_next_at = "pending", jobs.clock()
            row.delivery_token, row.delivery_until = None, None
            row.delivery_published_at, row.delivery_received_at = None, None
            row.delivery_recoveries += 1
            scheduled.append(identifier)
    return {"scheduled": scheduled, "unchanged": unchanged}


async def diagnostics(jobs) -> list[dict]:
    """Даёт только технические metadata; числа сообщений не выдаются за unique jobs."""
    async with jobs.sessions() as session:
        rows = await session.scalars(
            select(JobRow)
            .options(defer(JobRow.data))
            .where(JobRow.expires_at > jobs.clock())
            .order_by(JobRow.created_at)
            .limit(1000)
        )
        return [
            {
                "job_id": r.id,
                "kind": r.kind,
                "state": r.state,
                "expires_at": r.expires_at,
                "lease_until": r.lease_until,
                "delivery_state": r.delivery_state,
                "generation": r.delivery_generation,
                "attempts": r.delivery_attempts,
                "next_at": r.delivery_next_at,
                "confirmed_at": r.delivery_published_at,
                "error": r.delivery_error,
                "recoveries": r.delivery_recoveries,
                "duplicate_noops": r.delivery_noops,
            }
            for r in rows
        ]
