"""memory_items; messages.indexed_at outbox; family_memory permission

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "source_message_id",
            sa.Integer(),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", sa.String(32), nullable=False, server_default="note"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime()),
        sa.CheckConstraint(
            "scope IN ('user', 'family')", name="ck_memory_items_scope"
        ),
    )
    op.add_column("messages", sa.Column("indexed_at", sa.DateTime()))
    op.create_index("ix_messages_indexed_at", "messages", ["indexed_at"])
    # Grant family-memory write to the adult role; admin already has "*".
    op.execute(
        "UPDATE roles SET permissions = "
        'permissions || \'{"family_memory": true}\'::jsonb '
        "WHERE name = 'adult'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE roles SET permissions = permissions - 'family_memory' "
        "WHERE name = 'adult'"
    )
    op.drop_index("ix_messages_indexed_at", table_name="messages")
    op.drop_column("messages", "indexed_at")
    op.drop_table("memory_items")
