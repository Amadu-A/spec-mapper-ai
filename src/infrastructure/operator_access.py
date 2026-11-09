# src/infrastructure/operator_access.py
"""Одноразовые CLI tickets и отзыв коротких operator sessions без регистрации пользователей."""

from __future__ import annotations

import secrets
from uuid import uuid4

from sqlalchemy import delete, select, update

from src.domain.models import MapperError, digest
from src.infrastructure.database import CatalogAuditRow, OperatorTicketRow, ServiceActivityRow


class SqlOperatorAccess:
    """В БД только хеш ticket/session; raw ticket выдаётся лишь явной локальной CLI-командой."""

    def __init__(self, sessions, settings, clock):
        """Использует существующий SQL pool и server Settings без дополнительных runtime
        services.
        """
        self.sessions, self.settings, self.clock = sessions, settings, clock

    def audit(self, session, event: str, actor: str) -> None:
        """Аудит входа/выхода не содержит raw коды, cookies или имена пользователей."""
        session.add(
            CatalogAuditRow(id=str(uuid4()), at=self.clock(), actor=actor, event=event, details={})
        )

    async def issue(self, actor: str = "local_cli") -> str:
        """Генерирует ticket для CLI либо доверенного SSH gateway без запуска shell-команды."""
        ticket = secrets.token_urlsafe(32)
        async with self.sessions.begin() as session:
            session.add(
                OperatorTicketRow(
                    token_hash=digest(ticket),
                    expires_at=self.clock() + self.settings.operator_ticket_seconds,
                )
            )
            self.audit(session, "operator_ticket_issued", actor)
        return ticket

    async def consume(self, ticket: str) -> str:
        """CAS гарантирует одноразовость даже при двух API replicas; сессия имеет
        фиксированный TTL.
        """
        nonce = secrets.token_urlsafe(32)
        async with self.sessions.begin() as session:
            changed = await session.execute(
                update(OperatorTicketRow)
                .where(
                    OperatorTicketRow.token_hash == digest(ticket),
                    OperatorTicketRow.expires_at > self.clock(),
                    OperatorTicketRow.consumed_at.is_(None),
                )
                .values(consumed_at=self.clock())
            )
            if changed.rowcount != 1:
                raise MapperError(
                    "operator_ticket_invalid", "Код недействителен, использован или истёк", 401
                )
            session.add(
                ServiceActivityRow(name="operator:" + digest(nonce), last_seen=self.clock())
            )
            self.audit(session, "operator_session_started", "operator:" + digest(nonce))
        return nonce

    async def valid(self, nonce: str) -> bool:
        """Проверяет срок/отзыв сессии независимо от подписи cookie; обращение TTL не продлевает."""
        async with self.sessions() as session:
            created = await session.scalar(
                select(ServiceActivityRow.last_seen).where(
                    ServiceActivityRow.name == "operator:" + digest(nonce)
                )
            )
            return (
                created is not None
                and created + self.settings.operator_session_seconds > self.clock()
            )

    async def revoke(self, nonce: str) -> None:
        """Logout отзывает cookie серверно; повторно предъявленная подпись уже не даёт доступ."""
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ServiceActivityRow).where(
                    ServiceActivityRow.name == "operator:" + digest(nonce)
                )
            )
            self.audit(session, "operator_session_revoked", "operator:" + digest(nonce))
