"""messages.hidden — internal messages kept out of user-facing history

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "hidden",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE messages
        SET hidden = TRUE
        WHERE author_kind = 'user'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements(messages.parts) AS part(value)
              WHERE value ? 'checklist_answer'
          )
        """
    )
    op.execute(
        """
        UPDATE messages AS m
        SET hidden = TRUE
        FROM runs AS r
        JOIN messages AS trigger_msg ON trigger_msg.id = r.trigger_message_id
        WHERE m.run_id = r.id
          AND trigger_msg.hidden = TRUE
        """
    )


def downgrade() -> None:
    op.drop_column("messages", "hidden")
