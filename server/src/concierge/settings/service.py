from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.settings import crypto
from concierge.settings.models import Setting, SettingScope

# Single-worker in-process cache: (scope, scope_id, key) -> (version, stored value).
_cache: dict[tuple[SettingScope, int, str], tuple[int, Any]] = {}


def clear_cache() -> None:
    _cache.clear()


def _mask_all(values: dict[str, Any]) -> dict[str, Any]:
    return {k: crypto.mask_value(v) for k, v in values.items()}


async def list_scope(
    session: AsyncSession, scope: SettingScope, scope_id: int, *, reveal: bool = False
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Setting).where(Setting.scope == scope, Setting.scope_id == scope_id)
        )
    ).scalars()
    values: dict[str, Any] = {}
    for row in rows:
        _cache[(row.scope, row.scope_id, row.key)] = (row.version, row.value)
        values[row.key] = row.value
    return values if reveal else _mask_all(values)


async def _get_stored(
    session: AsyncSession, scope: SettingScope, scope_id: int, key: str
) -> Any:
    cached = _cache.get((scope, scope_id, key))
    if cached is not None:
        return cached[1]
    row = (
        await session.execute(
            select(Setting).where(
                Setting.scope == scope,
                Setting.scope_id == scope_id,
                Setting.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    _cache[(scope, scope_id, key)] = (row.version, row.value)
    return row.value


async def get_value(
    session: AsyncSession, user: User, key: str, default: Any = None
) -> Any:
    """Effective value: user scope -> role scope -> global."""
    for scope, scope_id in (
        (SettingScope.user, user.id),
        (SettingScope.role, user.role_id),
        (SettingScope.global_, 0),
    ):
        value = await _get_stored(session, scope, scope_id, key)
        if value is not None:
            if crypto.is_secret(value):
                return crypto.unwrap_secret(value)
            return value
    return default


async def set_value(
    session: AsyncSession,
    scope: SettingScope,
    scope_id: int,
    key: str,
    value: Any,
    updated_by: int | None,
) -> None:
    if isinstance(value, dict) and set(value) == {"secret"}:
        plaintext = value["secret"]
        if plaintext == crypto.SECRET_MASK:
            return  # unchanged masked secret
        value = crypto.wrap_secret(plaintext)
    row = (
        await session.execute(
            select(Setting).where(
                Setting.scope == scope,
                Setting.scope_id == scope_id,
                Setting.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = Setting(
            scope=scope, scope_id=scope_id, key=key, value=value, updated_by=updated_by
        )
        session.add(row)
    else:
        row.value = value
        row.version += 1
        row.updated_by = updated_by
    await session.commit()
    _cache[(scope, scope_id, key)] = (row.version, value)


async def delete_value(
    session: AsyncSession, scope: SettingScope, scope_id: int, key: str
) -> None:
    await session.execute(
        delete(Setting).where(
            Setting.scope == scope,
            Setting.scope_id == scope_id,
            Setting.key == key,
        )
    )
    await session.commit()
    _cache.pop((scope, scope_id, key), None)
