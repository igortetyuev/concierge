from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from concierge.core.db import Base

JSONBType = JSON().with_variant(JSONB(), "postgresql")


class ScheduledJob(Base):
    """A scheduled event/reminder that fires an agent run as its owner."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # command | reminder
    title: Mapped[str] = mapped_column(String(256), default="")
    text: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONBType, default=dict)
    # new — a fresh session per fire (daily or rarer / one-shot);
    # reuse — the job's own conversation is kept between fires.
    session_mode: Mapped[str] = mapped_column(String(8), default="new")
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    next_run_at: Mapped[datetime | None] = mapped_column(index=True)
    paused_until: Mapped[datetime | None] = mapped_column()
    last_run_at: Mapped[datetime | None] = mapped_column()
    fire_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
