"""allow 'schedule' in conversations.origin and messages.source_channel

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-18

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_conversations_origin", "conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_conversations_origin",
        "conversations",
        "origin IN ('web', 'voice_quick', 'telegram', 'schedule')",
    )
    op.drop_constraint(
        "ck_messages_source_channel", "messages", type_="check"
    )
    op.create_check_constraint(
        "ck_messages_source_channel",
        "messages",
        "source_channel IN ('web', 'telegram', 'schedule')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversations_origin", "conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_conversations_origin",
        "conversations",
        "origin IN ('web', 'voice_quick', 'telegram')",
    )
    op.drop_constraint(
        "ck_messages_source_channel", "messages", type_="check"
    )
    op.create_check_constraint(
        "ck_messages_source_channel",
        "messages",
        "source_channel IN ('web', 'telegram')",
    )
