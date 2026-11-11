from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from concierge.core.db import Base


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    linked_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    expires_at: Mapped[datetime] = mapped_column()


class TelegramBinding(Base):
    __tablename__ = "telegram_bindings"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('dm_current', 'topic', 'group')",
            name="ck_telegram_bindings_kind",
        ),
    )

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=0)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
