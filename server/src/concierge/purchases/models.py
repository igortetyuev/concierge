from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from concierge.core.db import Base


class PurchaseList(Base):
    """One persistent purchase list per owner and store (`""` = general)."""

    __tablename__ = "purchase_lists"
    __table_args__ = (
        UniqueConstraint("owner_id", "store_key", name="uq_purchase_lists_owner_store"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    store_key: Mapped[str] = mapped_column(String(128), default="")
    store_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class PurchaseListMember(Base):
    """Explicit access grant; the owner has implicit full rights."""

    __tablename__ = "purchase_list_members"

    list_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_lists.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))  # viewer | editor
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    __table_args__ = (
        Index("ix_purchase_items_list_status", "list_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_lists.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(256))
    canonical_key: Mapped[str] = mapped_column(String(256), index=True)
    category: Mapped[str] = mapped_column(String(64), default="прочее")
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit: Mapped[str] = mapped_column(String(32))
    unit_dimension: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    planned_for: Mapped[date] = mapped_column(Date, index=True)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_currency: Mapped[str] = mapped_column(String(8), default="RUB")
    price_kind: Mapped[str | None] = mapped_column(String(8))  # unit | total
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    bought_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    bought_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class PurchaseContext(Base):
    """Current store of a user; the service applies a four-hour expiry."""

    __tablename__ = "purchase_contexts"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    store_key: Mapped[str] = mapped_column(String(128))
    store_name: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
