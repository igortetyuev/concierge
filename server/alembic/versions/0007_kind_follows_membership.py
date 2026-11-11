"""conversation kind follows membership

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Group sessions that ended up with a single member become personal —
    # unless bound to a telegram group/topic chat, where unlinked chat
    # participants still write.
    op.execute(
        "UPDATE conversations SET kind = 'personal', reply_mode = 'always' "
        "WHERE kind = 'group' "
        "AND (SELECT count(*) FROM conversation_members m "
        "     WHERE m.conversation_id = conversations.id) <= 1 "
        "AND NOT EXISTS (SELECT 1 FROM telegram_bindings b "
        "                WHERE b.conversation_id = conversations.id "
        "                AND b.kind IN ('group', 'topic'))"
    )


def downgrade() -> None:
    pass
