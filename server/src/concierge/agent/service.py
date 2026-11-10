from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.models import CapabilityBinding

CAPABILITIES = (
    "chat",
    "chat_fast",
    "vision",
    "stt",
    "tts",
    "embedding",
    "document",
    "code_agent",
)


async def get_capability_binding(
    session: AsyncSession, capability: str, role_id: int | None = None
) -> CapabilityBinding | None:
    """Resolve a binding: role-specific row wins over the default (role_id NULL).

    Reads the DB on every call — binding changes take effect without restart.
    """
    if capability not in CAPABILITIES:
        return None
    stmt = (
        select(CapabilityBinding)
        .where(CapabilityBinding.capability == capability)
        .order_by(CapabilityBinding.role_id.is_(None))
    )
    if role_id is not None:
        stmt = stmt.where(
            CapabilityBinding.role_id.is_(None)
            | (CapabilityBinding.role_id == role_id)
        )
    else:
        stmt = stmt.where(CapabilityBinding.role_id.is_(None))
    return (await session.execute(stmt)).scalars().first()
