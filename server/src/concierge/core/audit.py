"""Append-only audit log for security-relevant actions."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from concierge.core.db import Base

JsonDict = dict[str, Any]
JSONBType = JSON().with_variant(JSONB(), "postgresql")


class AuditEvent(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


def record(
    session: AsyncSession,
    action: str,
    *,
    actor_id: int | None = None,
    actor_name: str = "",
    target: str = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
) -> None:
    """Enqueue an audit row; the caller's commit persists it."""
    session.add(
        AuditEvent(
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            target=target,
            detail=detail or {},
            ip=ip,
        )
    )
