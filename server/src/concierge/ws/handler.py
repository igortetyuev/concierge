from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from concierge.auth import security
from concierge.auth.models import User
from concierge.channels import ChannelEvent, Connection, get_hub
from concierge.channels.web import WebSocketSink
from concierge.conversations import service as conversations
from concierge.conversations.models import ConversationMember
from concierge.core.db import get_sessionmaker

router = APIRouter()

WS_CLOSE_UNAUTHORIZED = 4401


async def _authenticate(ws: WebSocket) -> User | None:
    token = ws.query_params.get("token")
    if not token:
        header = ws.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if not token:
        return None
    user_id = security.decode_access_token(token)
    if user_id is None:
        return None
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        return user


async def _send_error(ws: WebSocket, detail: str) -> None:
    await ws.send_json({"type": "error", "detail": detail})


async def _is_member(conversation_id: int, user_id: int) -> bool:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conversation_id,
                    ConversationMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        return row is not None


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    user = await _authenticate(ws)
    if user is None:
        await ws.close(code=WS_CLOSE_UNAUTHORIZED)
        return
    await ws.accept()
    hub = get_hub()
    conn = hub.connect(WebSocketSink(ws, user.id))
    await ws.send_json({"type": "hello", "user_id": user.id})
    try:
        while True:
            data = await ws.receive_json()
            await _handle(ws, conn, user, data)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(conn)


async def _handle(
    ws: WebSocket, conn: Connection, user: User, data: dict[str, Any]
) -> None:
    hub = get_hub()
    msg_type = data.get("type")
    conv_id = data.get("conversation_id")

    if msg_type not in ("subscribe", "unsubscribe", "typing", "read"):
        await _send_error(ws, f"unknown type: {msg_type}")
        return
    if not isinstance(conv_id, int):
        await _send_error(ws, "conversation_id required")
        return

    if msg_type == "subscribe":
        if not await _is_member(conv_id, user.id):
            await _send_error(ws, "not a member")
            return
        await hub.subscribe(conn, conv_id)
        await ws.send_json(
            {
                "type": "subscribed",
                "conversation_id": conv_id,
                "online_users": sorted(hub.online_users(conv_id)),
            }
        )
    elif msg_type == "unsubscribe":
        hub.unsubscribe(conn, conv_id)
        await ws.send_json({"type": "unsubscribed", "conversation_id": conv_id})
    elif msg_type == "typing":
        if conv_id in conn.subscriptions:
            await hub.publish(
                ChannelEvent("typing", conv_id, {"user_id": user.id}),
                exclude=conn,
            )
    elif msg_type == "read":
        seq = data.get("seq")
        if not isinstance(seq, int):
            await _send_error(ws, "seq required")
            return
        if conv_id not in conn.subscriptions:
            await _send_error(ws, "not subscribed")
            return
        async with get_sessionmaker()() as session:
            last_read = await conversations.mark_read(
                session, conv_id, user.id, seq
            )
        if last_read is not None:
            await hub.publish(
                ChannelEvent(
                    "read.updated",
                    conv_id,
                    {"user_id": user.id, "last_read_seq": last_read},
                )
            )
