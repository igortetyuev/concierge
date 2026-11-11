"""named_models.tools — per-model tool checklist for ask_model

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "named_models",
        sa.Column(
            "tools", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
    )


def downgrade() -> None:
    op.drop_column("named_models", "tools")
