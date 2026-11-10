import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelEvent:
    """Outbound event: message.created, message.delta, run.status, presence,
    typing, conversation.updated, read.updated."""

    type: str
    conversation_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class ChannelSink(Protocol):
    """One client connection (web WS now; Telegram chat later)."""

    user_id: int

    async def emit(self, event: ChannelEvent) -> None: ...


class Connection:
    __slots__ = ("sink", "subscriptions", "user_id")

    def __init__(self, sink: ChannelSink) -> None:
        self.sink = sink
        self.user_id = sink.user_id
        self.subscriptions: set[int] = set()


class Hub:
    """In-memory fanout. Single uvicorn worker; for >1 use LISTEN/NOTIFY."""

    def __init__(self) -> None:
        self._conns: set[Connection] = set()

    def connect(self, sink: ChannelSink) -> Connection:
        conn = Connection(sink)
        self._conns.add(conn)
        return conn

    async def disconnect(self, conn: Connection) -> None:
        self._conns.discard(conn)
        for conv_id in conn.subscriptions:
            if self._user_subscribed(conn.user_id, conv_id):
                continue
            await self.publish(
                ChannelEvent(
                    "presence",
                    conv_id,
                    {"user_id": conn.user_id, "online": False},
                )
            )

    async def subscribe(self, conn: Connection, conversation_id: int) -> None:
        already_online = self._user_subscribed(conn.user_id, conversation_id)
        conn.subscriptions.add(conversation_id)
        if not already_online:
            await self.publish(
                ChannelEvent(
                    "presence",
                    conversation_id,
                    {"user_id": conn.user_id, "online": True},
                )
            )

    def unsubscribe(self, conn: Connection, conversation_id: int) -> None:
        conn.subscriptions.discard(conversation_id)

    async def publish(
        self, event: ChannelEvent, exclude: Connection | None = None
    ) -> None:
        for conn in self._subscribed(event.conversation_id):
            if conn is not exclude:
                await self._emit(conn, event)

    async def publish_users(
        self, user_ids: set[int], event: ChannelEvent
    ) -> None:
        for conn in list(self._conns):
            if conn.user_id in user_ids:
                await self._emit(conn, event)

    def online_users(self, conversation_id: int) -> set[int]:
        return {
            conn.user_id for conn in self._subscribed(conversation_id)
        }

    def _subscribed(self, conversation_id: int | None) -> list[Connection]:
        return [
            conn
            for conn in self._conns
            if conversation_id in conn.subscriptions
        ]

    def _user_subscribed(self, user_id: int, conversation_id: int) -> bool:
        return any(
            conn.user_id == user_id and conversation_id in conn.subscriptions
            for conn in self._conns
        )

    async def _emit(self, conn: Connection, event: ChannelEvent) -> None:
        try:
            await conn.sink.emit(event)
        except Exception:
            log.warning("dropping dead sink user_id=%s", conn.user_id)
            self._conns.discard(conn)


_hub = Hub()


def get_hub() -> Hub:
    return _hub
