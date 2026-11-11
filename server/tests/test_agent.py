import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path

import httpx
from sqlalchemy import select
from starlette.testclient import TestClient

from concierge.agent.models import CapabilityBinding, McpServer, ModelProvider
from concierge.agent.providers import get_model
from concierge.agent.runtime import get_runtime
from concierge.api.app import create_app
from concierge.auth.models import Role
from concierge.conversations.models import Message, Run
from concierge.core import db
from tests.conftest import auth, login, make_user
from tests.test_ws import _connect_and_subscribe, _wait_type

MCP_ECHO_PATH = str(Path(__file__).parent / "mcp_echo_server.py")


_provider_seq = 0


async def _add_provider(
    capability: str = "chat",
    params: dict | None = None,
    kind: str = "test",
    role_id: int | None = None,
) -> None:
    global _provider_seq
    _provider_seq += 1
    params = dict(params or {})
    if capability == "chat":
        # Parallel tool calls share one sqlite connection (StaticPool);
        # create_document is covered by test_documents.py — keep it out
        # of the parallel batch so sibling rollbacks can't kill its write.
        params.setdefault(
            "call_tools",
            ["search_messages", "current_datetime", "remember", "recall", "forget"],
        )
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name=f"p-{capability}-{kind}-{_provider_seq}", kind=kind
        )
        session.add(provider)
        await session.flush()
        session.add(
            CapabilityBinding(
                capability=capability,
                provider_id=provider.id,
                model="test-model",
                params=params or {},
                role_id=role_id,
            )
        )
        await session.commit()


async def _post_and_collect(
    client: httpx.AsyncClient, token: str, conv_id: int, text: str
) -> dict:
    resp = await client.post(
        f"/conversations/{conv_id}/messages",
        json={"parts": [{"type": "text", "text": text}]},
        headers=auth(token),
    )
    assert resp.status_code == 201
    return resp.json()


async def _make_conv(
    client: httpx.AsyncClient, token: str, kind: str = "personal"
) -> int:
    resp = await client.post(
        "/conversations",
        json={"kind": kind, "title": "t", "member_ids": []},
        headers=auth(token),
    )
    assert resp.status_code == 201
    return int(resp.json()["id"])


async def _run_and_wait(conv_id: int, msg_id: int) -> Run:
    """Let the scheduled background run finish, then return the Run row."""
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(
                select(Run).where(Run.trigger_message_id == msg_id)
            )
        ).scalar_one()
        return run


async def test_get_model_resolves_binding(engine) -> None:
    await _add_provider("chat")
    user = await make_user("u1", "adult")
    async with db.get_sessionmaker()() as session:
        bound = await get_model(session, "chat", user)
    assert bound is not None
    assert bound.label.endswith("/test-model")


async def test_get_model_role_override(engine) -> None:
    await _add_provider("chat", params={"custom_output_text": "base"})
    user = await make_user("u2", "adult")
    async with db.get_sessionmaker()() as session:
        role_id = user.role_id
    await _add_provider("chat", params={"custom_output_text": "role"}, role_id=role_id)
    async with db.get_sessionmaker()() as session:
        bound = await get_model(session, "chat", user)
    assert bound is not None


async def test_no_binding_returns_none(engine) -> None:
    user = await make_user("u3", "adult")
    async with db.get_sessionmaker()() as session:
        assert await get_model(session, "chat", user) is None


async def test_agent_run_persists_and_uses_tools(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider("chat", params={"custom_output_text": "ответ"})
    await make_user("alice", "adult")
    token = await login(client, "alice")
    cid = await _make_conv(client, token)
    msg = await _post_and_collect(client, token, cid, "привет")

    run = await _run_and_wait(cid, msg["id"])
    assert run.status == "done"
    assert run.model is not None and run.model.endswith("/test-model")
    assert run.usage.get("requests", 0) >= 1

    async with db.get_sessionmaker()() as session:
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    assert reply.author_kind == "assistant"
    text_parts = [p for p in reply.parts if p["type"] == "text"]
    assert text_parts == [{"type": "text", "text": "ответ"}]
    # TestModel call_tools="all" -> both core tools were exposed and called.
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool"}
    assert {"search_messages", "current_datetime"} <= tool_names


async def test_tools_deny_list(engine, client: httpx.AsyncClient) -> None:
    await _add_provider(
        "chat",
        params={
            "custom_output_text": "ok",
            "call_tools": ["search_messages", "remember", "recall", "forget"],
        },
    )
    async with db.get_sessionmaker()() as session:
        session.add(
            Role(
                name="limited",
                permissions={
                    "tools": {"allow": ["*"], "deny": ["current_datetime"]},
                    "mcp_servers": ["*"],
                },
            )
        )
        await session.commit()
    await make_user("lim", "limited")
    token = await login(client, "lim")
    cid = await _make_conv(client, token)
    msg = await _post_and_collect(client, token, cid, "hi")

    run = await _run_and_wait(cid, msg["id"])
    async with db.get_sessionmaker()() as session:
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool"}
    assert "current_datetime" not in tool_names
    assert "search_messages" in tool_names


async def test_mcp_stdio_server_called(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider(
        "chat",
        params={
            "custom_output_text": "ok",
            "call_tools": [
                "search_messages",
                "current_datetime",
                "remember",
                "recall",
                "forget",
                "echo_srv_echo",
            ],
        },
    )
    async with db.get_sessionmaker()() as session:
        child_role = (
            await session.execute(select(Role).where(Role.name == "child"))
        ).scalar_one()
        session.add(
            McpServer(
                name="echo_srv",
                transport="stdio",
                command=f"{sys.executable} {MCP_ECHO_PATH}",
                enabled=True,
            )
        )
        await session.commit()
    # The child role can't see the MCP server; a role-scoped binding keeps
    # echo_srv_echo out of its TestModel call list (else KeyError).
    await _add_provider("chat", role_id=child_role.id)
    await make_user("mcpuser", "adult")
    token = await login(client, "mcpuser")
    cid = await _make_conv(client, token)
    msg = await _post_and_collect(client, token, cid, "hi")

    run = await _run_and_wait(cid, msg["id"])
    assert run.status == "done"
    async with db.get_sessionmaker()() as session:
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool"}
    assert "echo_srv_echo" in tool_names
    # Role child has mcp_servers=[] -> server not visible to that role.
    await make_user("kid", "child")
    token_k = await login(client, "kid")
    cid_k = await _make_conv(client, token_k)
    msg_k = await _post_and_collect(client, token_k, cid_k, "hi")
    run_k = await _run_and_wait(cid_k, msg_k["id"])
    async with db.get_sessionmaker()() as session:
        reply_k = (
            await session.execute(
                select(Message).where(Message.run_id == run_k.id)
            )
        ).scalar_one()
    tool_names_k = {p["name"] for p in reply_k.parts if p["type"] == "tool"}
    assert "echo_srv_echo" not in tool_names_k


async def test_reply_mode_mention(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("chat", params={"custom_output_text": "ok"})
    await make_user("own", "adult")
    token = await login(client, "own")
    resp = await client.post(
        "/conversations",
        json={"kind": "group", "title": "g", "member_ids": []},
        headers=auth(token),
    )
    cid = int(resp.json()["id"])
    assert resp.json()["reply_mode"] == "mention"

    await _post_and_collect(client, token, cid, "просто чат")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        runs = (
            await session.execute(
                select(Run).where(Run.conversation_id == cid)
            )
        ).scalars()
        assert list(runs) == []

    msg_yes = await _post_and_collect(client, token, cid, "@консьерж привет")
    run = await _run_and_wait(cid, msg_yes["id"])
    assert run.status == "done"


async def test_e2e_stream_over_ws(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("chat", params={"custom_output_text": "стрим"})
    await make_user("alice2", "adult")
    token = await login(client, "alice2")
    cid = await _make_conv(client, token)

    def scenario() -> None:
        with TestClient(create_app()) as tc, ExitStack() as stack:
            ws = _connect_and_subscribe(tc, stack, token, cid)
            assert _wait_type(ws, "subscribed")["conversation_id"] == cid
            resp = tc.post(
                f"/conversations/{cid}/messages",
                json={"parts": [{"type": "text", "text": "привет"}]},
                headers=auth(token),
            )
            assert resp.status_code == 201
            saw_delta = False
            reply = None
            for _ in range(50):
                ev = ws.receive_json()
                if ev["type"] == "message.delta":
                    saw_delta = True
                if (
                    ev["type"] == "message.created"
                    and ev["message"]["author_kind"] == "assistant"
                ):
                    reply = ev["message"]
                    break
            assert saw_delta
            assert reply is not None
            text_parts = [p for p in reply["parts"] if p["type"] == "text"]
            assert text_parts[-1]["text"] == "стрим"
            assert reply["run_id"] is not None

            # Persisted: user message then assistant reply (checked via REST —
            # TestClient's lifespan disposes the shared engine on exit).
            msgs = tc.get(
                f"/conversations/{cid}/messages", headers=auth(token)
            ).json()
            kinds = [m["author_kind"] for m in msgs]
            assert kinds == ["user", "assistant"]

    await asyncio.to_thread(scenario)
