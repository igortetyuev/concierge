from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth import security
from concierge.auth.models import Device, Invite, Role, User
from concierge.core.config import get_settings


async def authenticate(
    session: AsyncSession, username: str, password: str
) -> User | None:
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    if not security.verify_password(user.password_hash, password):
        return None
    return user


async def create_device(
    session: AsyncSession, user: User, name: str
) -> tuple[Device, str]:
    refresh_token = security.new_refresh_token()
    device = Device(
        user_id=user.id,
        name=name,
        refresh_token_hash=security.hash_token(refresh_token),
    )
    session.add(device)
    await session.commit()
    return device, refresh_token


def _refresh_expired(device: Device) -> bool:
    ttl = timedelta(days=get_settings().refresh_token_ttl_days)
    created = device.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) > created + ttl


async def rotate_refresh_token(
    session: AsyncSession, refresh_token: str
) -> tuple[User, Device, str] | None:
    device = (
        await session.execute(
            select(Device).where(
                Device.refresh_token_hash == security.hash_token(refresh_token)
            )
        )
    ).scalar_one_or_none()
    if device is None or device.revoked_at is not None or _refresh_expired(device):
        return None
    user = await session.get(User, device.user_id)
    if user is None or not user.is_active:
        return None
    new_token = security.new_refresh_token()
    device.refresh_token_hash = security.hash_token(new_token)
    device.last_seen_at = datetime.now(UTC)
    await session.commit()
    return user, device, new_token


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> None:
    device = (
        await session.execute(
            select(Device).where(
                Device.refresh_token_hash == security.hash_token(refresh_token)
            )
        )
    ).scalar_one_or_none()
    if device is not None and device.revoked_at is None:
        device.revoked_at = datetime.now(UTC)
        await session.commit()


async def create_invite(
    session: AsyncSession, role_id: int, created_by: int
) -> tuple[Invite, str] | None:
    if await session.get(Role, role_id) is None:
        return None
    token = security.new_invite_token()
    invite = Invite(
        token_hash=security.hash_token(token),
        role_id=role_id,
        created_by=created_by,
        expires_at=datetime.now(UTC)
        + timedelta(hours=get_settings().invite_ttl_hours),
    )
    session.add(invite)
    await session.commit()
    return invite, token


async def register_with_invite(
    session: AsyncSession,
    token: str,
    username: str,
    display_name: str,
    password: str,
) -> User | None:
    invite = (
        await session.execute(
            select(Invite).where(Invite.token_hash == security.hash_token(token))
        )
    ).scalar_one_or_none()
    if invite is None or invite.used_by is not None:
        return None
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires:
        return None
    exists = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if exists is not None:
        return None
    user = User(
        username=username,
        display_name=display_name,
        role_id=invite.role_id,
        password_hash=security.hash_password(password),
    )
    session.add(user)
    await session.flush()
    invite.used_by = user.id
    await session.commit()
    await session.refresh(user, attribute_names=["role"])
    return user
