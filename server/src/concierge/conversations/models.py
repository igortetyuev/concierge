from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from concierge.auth.models import JsonDict
from concierge.core.db import Base

JSONBType = JSON().with_variant(JSONB(), "postgresql")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # personal | group
    title: Mapped[str] = mapped_column(String(256), default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    reply_mode: Mapped[str] = mapped_column(String(16), default="always")
    origin: Mapped[str] = mapped_column(String(16), default="web")
    summary: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column()

    members: Mapped[list["ConversationMember"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ConversationMember(Base):
    __tablename__ = "conversation_members"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_read_seq: Mapped[int] = mapped_column(default=0)

    conversation: Mapped[Conversation] = relationship(back_populates="members")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    trigger_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", use_alter=True, name="fk_runs_trigger_message")
    )
    status: Mapped[str] = mapped_column(String(16), default="queued")
    model: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conv_seq"),
        UniqueConstraint(
            "conversation_id",
            "author_id",
            "client_msg_id",
            name="uq_messages_client_msg",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column()
    author_kind: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    parts: Mapped[list[dict[str, Any]]] = mapped_column(JSONBType, default=list)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    source_channel: Mapped[str] = mapped_column(String(16), default="web")
    client_msg_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Postgres-only generated column `tsv tsvector` lives in the migration
    # (GENERATED ALWAYS AS to_tsvector('simple', parts)); not mapped here
    # because SQLite has no tsvector.
