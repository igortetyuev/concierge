"""telegram_links, telegram_link_codes, telegram_bindings, telegram_messages

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_links",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("telegram_username", sa.String(64)),
        sa.Column(
            "linked_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_telegram_links_telegram_user_id",
        "telegram_links",
        ["telegram_user_id"],
    )
    op.create_table(
        "telegram_link_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "telegram_bindings",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.BigInteger(),
            primary_key=True,
            server_default="0",
        ),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "kind IN ('dm_current', 'topic', 'group')",
            name="ck_telegram_bindings_kind",
        ),
    )
    op.create_table(
        "telegram_messages",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "conversation_message_id",
            sa.Integer(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
    )


def downgrade() -> None:
    for table in (
        "telegram_messages",
        "telegram_bindings",
        "telegram_link_codes",
        "telegram_links",
    ):
        op.drop_table(table)
