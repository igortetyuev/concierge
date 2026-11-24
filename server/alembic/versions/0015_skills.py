"""skills + skill_files — SKILL.md documents with optional text files

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.JSONB(), nullable=False),
        sa.Column("triggers", postgresql.JSONB(), nullable=False),
        sa.Column("argument_hint", sa.String(length=256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("role_ids", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skills_owner_id", "skills", ["owner_id"])
    op.create_index(
        "uq_skills_shared_name",
        "skills",
        ["name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NULL"),
    )
    op.create_index(
        "uq_skills_owner_name",
        "skills",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NOT NULL"),
    )
    op.create_table(
        "skill_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("skill_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "path", name="uq_skill_files_path"),
    )
    op.create_index("ix_skill_files_skill_id", "skill_files", ["skill_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_files_skill_id", table_name="skill_files")
    op.drop_table("skill_files")
    op.drop_index("uq_skills_owner_name", table_name="skills")
    op.drop_index("uq_skills_shared_name", table_name="skills")
    op.drop_index("ix_skills_owner_id", table_name="skills")
    op.drop_table("skills")
