"""users, roles, invites, devices, settings, providers, bindings, mcp, code_agents

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ROLES = [
    (
        "admin",
        {"*": True, "max_upload_mb": 200},
        "Ты — семейный консьерж. Пользователь — администратор.",
    ),
    (
        "adult",
        {
            "settings": {"user": "rw", "global": "r"},
            "manage_users": False,
            "tools": {"allow": ["*"], "deny": []},
            "mcp_servers": ["*"],
            "capabilities_override": {},
            "max_upload_mb": 100,
            "code_agent": True,
            "telegram": True,
            "voice_quick_session": True,
        },
        "Ты — семейный консьерж. Пользователь — взрослый член семьи.",
    ),
    (
        "child",
        {
            "settings": {"user": "rw", "global": "r"},
            "manage_users": False,
            "tools": {"allow": ["*"], "deny": []},
            "mcp_servers": [],
            "capabilities_override": {},
            "max_upload_mb": 50,
            "code_agent": False,
            "telegram": True,
            "voice_quick_session": True,
        },
        "Ты — семейный консьерж. Пользователь — ребёнок; отвечай просто и безопасно.",
    ),
    (
        "guest",
        {
            "settings": {"user": "r"},
            "manage_users": False,
            "tools": {"allow": [], "deny": ["*"]},
            "mcp_servers": [],
            "capabilities_override": {},
            "max_upload_mb": 10,
            "code_agent": False,
            "telegram": False,
            "voice_quick_session": False,
        },
        "Ты — семейный консьерж. Пользователь — гость.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "permissions", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("prompt_fragment", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False
        ),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prompt_fragment", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "token_hash", sa.String(64), nullable=False, unique=True, index=True
        ),
        sa.Column(
            "role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False
        ),
        sa.Column(
            "created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "refresh_token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime()),
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("scope", "scope_id", "key", name="uq_settings_scope_key"),
        sa.CheckConstraint(
            "scope IN ('global', 'role', 'user')", name="ck_settings_scope"
        ),
    )
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(512)),
        sa.Column("api_key_ref", sa.String(128)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "capability_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("capability", sa.String(32), nullable=False, index=True),
        sa.Column(
            "provider_id",
            sa.Integer(),
            sa.ForeignKey("model_providers.id"),
            nullable=False,
        ),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id")),
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("command", sa.String(512)),
        sa.Column("url", sa.String(512)),
        sa.Column("env_ref", sa.String(128)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "allowed_tools",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_table(
        "code_agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("ssh_host", sa.String(128), nullable=False),
        sa.Column("workdir", sa.String(512), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )

    roles = sa.table(
        "roles",
        sa.column("name", sa.String),
        sa.column("permissions", postgresql.JSONB),
        sa.column("prompt_fragment", sa.Text),
    )
    op.bulk_insert(
        roles,
        [
            {"name": name, "permissions": perms, "prompt_fragment": prompt}
            for name, perms, prompt in ROLES
        ],
    )


def downgrade() -> None:
    for table in (
        "code_agents",
        "mcp_servers",
        "capability_bindings",
        "model_providers",
        "settings",
        "devices",
        "invites",
        "users",
        "roles",
    ):
        op.drop_table(table)
