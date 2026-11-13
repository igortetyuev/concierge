"""voices + voice_samples (speaker-ID)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("centroid", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "sample_attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
        ),
        sa.Column("last_heard_at", sa.DateTime()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "voice_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "voice_id",
            sa.Integer(),
            sa.ForeignKey("voices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attachment_id",
            sa.Integer(),
            sa.ForeignKey("attachments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("embedding", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("attachment_id", name="uq_voice_samples_attachment_id"),
    )
    op.create_index("ix_voice_samples_voice_id", "voice_samples", ["voice_id"])


def downgrade() -> None:
    op.drop_table("voice_samples")
    op.drop_table("voices")
