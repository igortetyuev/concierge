"""voices.authenticated — treat the voice's audio as authored by its user

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "voices",
        sa.Column(
            "authenticated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("voices", "authenticated")
