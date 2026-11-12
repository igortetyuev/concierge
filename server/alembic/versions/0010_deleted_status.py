"""conversations.status += 'deleted' (soft-delete)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_conversations_status", "conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_conversations_status",
        "conversations",
        "status IN ('active', 'archived', 'deleted')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversations_status", "conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_conversations_status",
        "conversations",
        "status IN ('active', 'archived')",
    )
