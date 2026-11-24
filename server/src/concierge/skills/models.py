from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from concierge.core.db import Base

JSONBType = JSON().with_variant(JSONB(), "postgresql")


class Skill(Base):
    """A SKILL.md document (agentskills.io format) stored in the DB.

    owner_id NULL — a shared skill managed by admins and granted to roles;
    otherwise a personal skill visible only to its owner (a personal skill
    shadows a shared one with the same name).
    """

    __tablename__ = "skills"
    __table_args__ = (
        Index(
            "uq_skills_shared_name",
            "name",
            unique=True,
            sqlite_where=text("owner_id IS NULL"),
            postgresql_where=text("owner_id IS NULL"),
        ),
        Index(
            "uq_skills_owner_name",
            "owner_id",
            "name",
            unique=True,
            sqlite_where=text("owner_id IS NOT NULL"),
            postgresql_where=text("owner_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    aliases: Mapped[list[str]] = mapped_column(JSONBType, default=list)
    triggers: Mapped[list[str]] = mapped_column(JSONBType, default=list)
    argument_hint: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    # Role ids allowed to see a shared skill; [] or ["*"] = all roles.
    role_ids: Mapped[list[Any]] = mapped_column(JSONBType, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    files: Mapped[list["SkillFile"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class SkillFile(Base):
    """A text file attached to a skill (references/*, scripts/* as text)."""

    __tablename__ = "skill_files"
    __table_args__ = (
        UniqueConstraint("skill_id", "path", name="uq_skill_files_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)

    skill: Mapped[Skill] = relationship(back_populates="files")
