import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Enum as SAEnum

from concierge.core.db import Base

JsonValue = Any
JSONBType = JSON().with_variant(JSONB(), "postgresql")


class SettingScope(enum.StrEnum):
    global_ = "global"
    role = "role"
    user = "user"


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", name="uq_settings_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[SettingScope] = mapped_column(
        SAEnum(SettingScope, native_enum=False, length=16)
    )
    # 0 for global; role_id / user_id otherwise.
    scope_id: Mapped[int] = mapped_column(default=0)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[JsonValue] = mapped_column(JSONBType)
    version: Mapped[int] = mapped_column(default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
