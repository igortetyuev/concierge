from datetime import datetime

from sqlalchemy import JSON, BigInteger, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from concierge.auth.models import JsonDict
from concierge.core.db import Base

JSONBType = JSON().with_variant(JSONB(), "postgresql")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    original_name: Mapped[str] = mapped_column(String(512), default="")
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    derived: Mapped[JsonDict] = mapped_column(JSONBType, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Voice(Base):
    __tablename__ = "voices"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    authenticated: Mapped[bool] = mapped_column(default=False)
    centroid: Mapped[list[float]] = mapped_column(JSONBType, default=list)
    sample_count: Mapped[int] = mapped_column(default=0)
    sample_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL")
    )
    last_heard_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VoiceSample(Base):
    __tablename__ = "voice_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    voice_id: Mapped[int] = mapped_column(
        ForeignKey("voices.id", ondelete="CASCADE"), index=True
    )
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), unique=True
    )
    embedding: Mapped[list[float]] = mapped_column(JSONBType, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
