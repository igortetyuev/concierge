"""Import all ORM models so Base.metadata is complete (alembic, tests)."""

from concierge.agent.models import (
    CapabilityBinding,
    CodeAgent,
    McpServer,
    ModelProvider,
)
from concierge.auth.models import Device, Invite, Role, User
from concierge.channels.telegram.models import (
    TelegramBinding,
    TelegramLink,
    TelegramLinkCode,
    TelegramMessage,
)
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
    Run,
)
from concierge.core.audit import AuditEvent
from concierge.media.models import Attachment
from concierge.memory.models import MemoryItem
from concierge.schedule.models import ScheduledJob
from concierge.settings.models import Setting, SettingScope
from concierge.skills.models import Skill, SkillFile

__all__ = [
    "Attachment",
    "AuditEvent",
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
    "ScheduledJob",
    "Setting",
    "SettingScope",
    "Skill",
    "SkillFile",
    "TelegramBinding",
    "TelegramLink",
    "TelegramLinkCode",
    "TelegramMessage",
    "User",
]
