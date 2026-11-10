"""Import all ORM models so Base.metadata is complete (alembic, tests)."""

from concierge.agent.models import (
    CapabilityBinding,
    CodeAgent,
    McpServer,
    ModelProvider,
)
from concierge.auth.models import Device, Invite, Role, User
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
    Run,
)
from concierge.media.models import Attachment
from concierge.memory.models import MemoryItem
from concierge.settings.models import Setting, SettingScope

__all__ = [
    "Attachment",
    "CapabilityBinding",
    "CodeAgent",
    "Conversation",
    "ConversationMember",
    "Device",
    "Invite",
    "McpServer",
    "MemoryItem",
    "Message",
    "ModelProvider",
    "Role",
    "Run",
    "Setting",
    "SettingScope",
    "User",
]
