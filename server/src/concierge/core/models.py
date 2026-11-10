"""Import all ORM models so Base.metadata is complete (alembic, tests)."""

from concierge.agent.models import (
    CapabilityBinding,
    CodeAgent,
    McpServer,
    ModelProvider,
)
from concierge.auth.models import Device, Invite, Role, User
from concierge.settings.models import Setting, SettingScope

__all__ = [
    "CapabilityBinding",
    "CodeAgent",
    "Device",
    "Invite",
    "McpServer",
    "ModelProvider",
    "Role",
    "Setting",
    "SettingScope",
    "User",
]
