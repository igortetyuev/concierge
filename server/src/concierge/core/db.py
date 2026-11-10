import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from concierge.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def json_serializer(obj: Any) -> str:
    """ensure_ascii=False keeps Cyrillic searchable via LIKE on sqlite."""
    return json.dumps(obj, ensure_ascii=False)


def utcnow() -> datetime:
    """Naive UTC now — ORM columns are TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(UTC).replace(tzinfo=None)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url, json_serializer=json_serializer
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def set_engine(engine: AsyncEngine | None) -> None:
    """Replace the global engine (tests)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = None


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    _sessionmaker = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None
