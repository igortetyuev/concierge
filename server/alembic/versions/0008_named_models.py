"""named_models — models callable by name via the ask_model tool

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "named_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "provider_id",
            sa.Integer(),
            sa.ForeignKey("model_providers.id"),
            nullable=False,
        ),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("system_prompt", sa.String(2048)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_table("named_models")
