from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.models import CapabilityBinding, NamedModel

CAPABILITIES = (
    "chat",
    "chat_fast",
    "vision",
    "stt",
    "tts",
    "embedding",
    "document",
    "code_agent",
    "speaker",
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


async def enabled_named_model_names(session: AsyncSession) -> list[str]:
    """Names of enabled named models, for the ask_model tool enum."""
    rows = (
        await session.execute(
            select(NamedModel.name)
            .where(NamedModel.enabled.is_(True))
            .order_by(NamedModel.id)
        )
    ).scalars()
    return list(rows)


async def get_named_model(
    session: AsyncSession, name: str
) -> NamedModel | None:
    """Find an enabled named model by name, case-insensitive.

    Matched in Python — SQLite lower() is ASCII-only, and the table is tiny.
    """
    rows = (
        await session.execute(
            select(NamedModel).where(NamedModel.enabled.is_(True))
        )
    ).scalars()
    needle = name.strip().lower()
    return next((m for m in rows if m.name.lower() == needle), None)
