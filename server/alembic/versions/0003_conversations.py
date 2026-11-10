"""conversations, members, messages (tsv FTS), runs, attachments

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "reply_mode", sa.String(16), nullable=False, server_default="always"
        ),
        sa.Column("origin", sa.String(16), nullable=False, server_default="web"),
        sa.Column("summary", sa.Text()),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("archived_at", sa.DateTime()),
        sa.CheckConstraint(
            "kind IN ('personal', 'group')", name="ck_conversations_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_conversations_status"
        ),
        sa.CheckConstraint(
            "reply_mode IN ('always', 'mention')",
            name="ck_conversations_reply_mode",
        ),
        sa.CheckConstraint(
            "origin IN ('web', 'voice_quick', 'telegram')",
            name="ck_conversations_origin",
        ),
    )
    op.create_table(
        "conversation_members",
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "joined_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_read_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("trigger_message_id", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("model", sa.String(128)),
        sa.Column("usage", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("parts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id")),
        sa.Column(
            "source_channel", sa.String(16), nullable=False, server_default="web"
        ),
        sa.Column("client_msg_id", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', parts)", persisted=True),
        ),
        sa.UniqueConstraint(
            "conversation_id", "seq", name="uq_messages_conv_seq"
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "author_id",
            "client_msg_id",
            name="uq_messages_client_msg",
        ),
        sa.CheckConstraint(
            "author_kind IN ('user', 'assistant', 'system')",
            name="ck_messages_author_kind",
        ),
        sa.CheckConstraint(
            "source_channel IN ('web', 'telegram')",
            name="ck_messages_source_channel",
        ),
    )
    op.create_index(
        "ix_messages_tsv", "messages", ["tsv"], postgresql_using="gin"
    )
    # Deferred: FK cycle messages <-> runs.
    op.create_foreign_key(
        "fk_runs_trigger_message",
        "runs",
        "messages",
        ["trigger_message_id"],
        ["id"],
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, index=True),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("original_name", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "uploaded_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        ),
        sa.Column("derived", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    for table in (
        "attachments",
        "messages",
        "runs",
        "conversation_members",
        "conversations",
    ):
        op.drop_table(table)
