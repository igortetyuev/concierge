"""seed the shared purchases skill

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from concierge.purchases.default_skill import DEFAULT_SKILL_CONTENT
from concierge.skills.format import parse_skill_md

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _skills() -> sa.Table:
    return sa.table(
        "skills",
        sa.column("owner_id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("aliases", sa.JSON),
        sa.column("triggers", sa.JSON),
        sa.column("argument_hint", sa.String),
        sa.column("ephemeral", sa.Boolean),
        sa.column("content", sa.Text),
        sa.column("role_ids", sa.JSON),
        sa.column("enabled", sa.Boolean),
        sa.column("version", sa.Integer),
        sa.column("created_by", sa.Integer),
    )


def upgrade() -> None:
    doc = parse_skill_md(DEFAULT_SKILL_CONTENT)
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT id FROM skills WHERE owner_id IS NULL AND name = :name"),
        {"name": doc.name},
    ).first()
    if exists is not None:
        # An existing row may have been customized by an administrator; never
        # overwrite it from a schema migration.
        return
    conn.execute(
        _skills().insert().values(
            owner_id=None,
            name=doc.name,
            description=doc.description,
            aliases=doc.aliases,
            triggers=doc.triggers,
            argument_hint=doc.argument_hint,
            ephemeral=doc.ephemeral,
            content=doc.content,
            role_ids=[],
            enabled=True,
            version=1,
            created_by=None,
        )
    )


def _json_value(value: object) -> object:
    if isinstance(value, str):
        import json

        return json.loads(value)
    return value


def downgrade() -> None:
    # Remove only the untouched seeded row. If the installation customized its
    # text, ownership/roles or flags, downgrading the schema migration must not
    # silently destroy that configuration.
    doc = parse_skill_md(DEFAULT_SKILL_CONTENT)
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            "SELECT id, description, aliases, triggers, argument_hint, ephemeral, "
            "content, role_ids, enabled, version, created_by "
            "FROM skills WHERE owner_id IS NULL AND name = :name"
        ),
        {"name": doc.name},
    ).mappings().first()
    if row is None:
        return
    untouched = (
        row["description"] == doc.description
        and _json_value(row["aliases"]) == doc.aliases
        and _json_value(row["triggers"]) == doc.triggers
        and row["argument_hint"] == doc.argument_hint
        and bool(row["ephemeral"]) == doc.ephemeral
        and row["content"] == doc.content
        and _json_value(row["role_ids"]) == []
        and bool(row["enabled"]) is True
        and int(row["version"]) == 1
        and row["created_by"] is None
    )
    if untouched:
        conn.execute(sa.text("DELETE FROM skills WHERE id = :id"), {"id": row["id"]})
