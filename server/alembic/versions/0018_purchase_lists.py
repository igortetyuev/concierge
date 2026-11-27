"""purchase lists, items, sharing members and current-store contexts

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "purchase_lists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column(
            "store_key",
            sa.String(length=128),
            server_default="",
            nullable=False,
        ),
        sa.Column("store_name", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "store_key", name="uq_purchase_lists_owner_store"
        ),
    )
    op.create_table(
        "purchase_list_members",
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["list_id"], ["purchase_lists.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("list_id", "user_id"),
    )
    op.create_table(
        "purchase_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("canonical_key", sa.String(length=256), nullable=False),
        sa.Column(
            "category",
            sa.String(length=64),
            server_default="прочее",
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("unit_dimension", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("planned_for", sa.Date(), nullable=False),
        sa.Column("price_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "price_currency",
            sa.String(length=8),
            server_default="RUB",
            nullable=False,
        ),
        sa.Column("price_kind", sa.String(length=8), nullable=True),
        sa.Column("assigned_to", sa.Integer(), nullable=True),
        sa.Column("assigned_by", sa.Integer(), nullable=True),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("bought_by", sa.Integer(), nullable=True),
        sa.Column("bought_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"]),
        sa.ForeignKeyConstraint(["bought_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["list_id"], ["purchase_lists.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_purchase_items_list_status",
        "purchase_items",
        ["list_id", "status"],
    )
    op.create_index(
        "ix_purchase_items_canonical_key",
        "purchase_items",
        ["canonical_key"],
    )
    op.create_index(
        "ix_purchase_items_assigned_to",
        "purchase_items",
        ["assigned_to"],
    )
    op.create_index(
        "ix_purchase_items_planned_for",
        "purchase_items",
        ["planned_for"],
    )
    op.create_table(
        "purchase_contexts",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("store_key", sa.String(length=128), nullable=False),
        sa.Column("store_name", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("purchase_contexts")
    op.drop_index("ix_purchase_items_planned_for", table_name="purchase_items")
    op.drop_index("ix_purchase_items_assigned_to", table_name="purchase_items")
    op.drop_index("ix_purchase_items_canonical_key", table_name="purchase_items")
    op.drop_index("ix_purchase_items_list_status", table_name="purchase_items")
    op.drop_table("purchase_items")
    op.drop_table("purchase_list_members")
    op.drop_table("purchase_lists")
