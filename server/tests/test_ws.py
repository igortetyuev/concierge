import asyncio
from contextlib import ExitStack

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from concierge.api.app import create_app
from tests.conftest import auth, login, make_user


def _wait_type(ws: WebSocketTestSession, msg_type: str, limit: int = 10) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"no {msg_type} in {limit} messages")


def _connect_and_subscribe(
    tc: TestClient,
    stack: ExitStack,
    token: str,
    conversation_id: int,
) -> WebSocketTestSession:
    ws = stack.enter_context(tc.websocket_connect(f"/ws?token={token}"))
    assert ws.receive_json()["type"] == "hello"
    ws.send_json({"type": "subscribe", "conversation_id": conversation_id})
    return ws


async def test_ws_fanout_message_created(
    engine, client: httpx.AsyncClient
) -> None:
    alice = await make_user("alice", "adult")
    bob = await make_user("bob", "adult")
    await make_user("carol", "adult")
    token_a = await login(client, "alice")
    token_b = await login(client, "bob")
    token_c = await login(client, "carol")
    conv = (
        await client.post(
            "/conversations",
            json={"kind": "group", "title": "g", "member_ids": [bob.id]},
            headers=auth(token_a),
        )
    ).json()
    cid = conv["id"]

    def scenario() -> None:
        with TestClient(create_app()) as tc, ExitStack() as stack:
            ws_a1 = _connect_and_subscribe(tc, stack, token_a, cid)
            ws_a2 = _connect_and_subscribe(tc, stack, token_a, cid)
            ws_b = _connect_and_subscribe(tc, stack, token_b, cid)
            for ws in (ws_a1, ws_a2, ws_b):
                assert _wait_type(ws, "subscribed")["conversation_id"] == cid

            # Non-member cannot subscribe.
            ws_c = stack.enter_context(
                tc.websocket_connect(f"/ws?token={token_c}")
            )
            ws_c.receive_json()  # hello
            ws_c.send_json({"type": "subscribe", "conversation_id": cid})
            assert _wait_type(ws_c, "error")["detail"] == "not a member"
            ws_c.close()

            # One posted message -> one message.created on every subscribed
            # connection (two devices of alice + bob).
            resp = tc.post(
                f"/conversations/{cid}/messages",
                json={"parts": [{"type": "text", "text": "всем привет"}]},
                headers=auth(token_a),
            )
            assert resp.status_code == 201
            for ws in (ws_a1, ws_a2, ws_b):
                ev = _wait_type(ws, "message.created")
                assert ev["conversation_id"] == cid
                assert ev["message"]["seq"] == 1
                assert ev["message"]["author_id"] == alice.id

            # Second message: seq 2 proves no duplicated fanout of seq 1.
            tc.post(
                f"/conversations/{cid}/messages",
                json={"parts": [{"type": "text", "text": "снова"}]},
                headers=auth(token_b),
            )
            for ws in (ws_a1, ws_a2, ws_b):
                ev = _wait_type(ws, "message.created")
                assert ev["message"]["seq"] == 2
                assert ev["message"]["author_id"] == bob.id

            # Typing reaches other connections, not the sender.
            ws_a1.send_json({"type": "typing", "conversation_id": cid})
            assert _wait_type(ws_b, "typing")["user_id"] == alice.id
            assert _wait_type(ws_a2, "typing")["user_id"] == alice.id

            # Read marker fans out to the room.
            ws_b.send_json(
                {"type": "read", "conversation_id": cid, "seq": 2}
            )
            ev = _wait_type(ws_a1, "read.updated")
            assert ev["user_id"] == bob.id
            assert ev["last_read_seq"] == 2

    await asyncio.to_thread(scenario)


async def test_ws_rejects_bad_token(engine, client: httpx.AsyncClient) -> None:
    def scenario() -> None:
        tc = TestClient(create_app())
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            tc.websocket_connect("/ws?token=not-a-token"),
        ):
            pass
        assert exc.value.code == 4401

    await asyncio.to_thread(scenario)
