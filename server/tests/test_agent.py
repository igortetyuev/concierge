import asyncio
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import httpx
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import func, select
from starlette.testclient import TestClient

from concierge.agent.models import (
    CapabilityBinding,
    McpServer,
    ModelProvider,
    NamedModel,
)
from concierge.agent.providers import get_model
from concierge.agent.runtime import get_runtime
from concierge.api.app import create_app
from concierge.auth.models import Role, User
from concierge.conversations.models import Conversation, Message, Run
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


async def _add_named_model(
    name: str = "Алиса",
    params: dict | None = None,
    provider_kind: str = "test",
    enabled: bool = True,
    tools: list[str] | None = None,
) -> None:
    global _provider_seq
    _provider_seq += 1
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name=f"p-named-{_provider_seq}", kind=provider_kind
        )
        session.add(provider)
        await session.flush()
        session.add(
            NamedModel(
                name=name,
                provider_id=provider.id,
                model="test-model",
                params=params or {},
                tools=tools or [],
                enabled=enabled,
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


async def test_web_capability_permission_gate() -> None:
    from concierge.agent import tools

    allow_all = {"tools": {"allow": ["*"], "deny": []}}
    assert "web_search" in tools.allowed_tool_names(allow_all)
    assert "fetch_url" in tools.allowed_tool_names(allow_all)
    assert len(tools.allowed_capabilities(allow_all)) == 1

    denied = {"tools": {"allow": ["*"], "deny": ["web_search"]}}
    assert "web_search" not in tools.allowed_tool_names(denied)
    assert tools.allowed_capabilities(denied) == []
    # A denied capability name must not leak into plain tools either.
    assert "web_search" not in {
        f.__name__ for f in tools.core_tools(denied)
    }


async def test_fetch_url_ssrf_guard() -> None:
    from concierge.agent.tools import fetch_url

    for url in (
        "http://127.0.0.1:6333/",
        "http://localhost/",
        "http://192.168.99.66:11434/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "ftp://example.com/x",
        "file:///etc/passwd",
    ):
        res = await fetch_url(url)
        assert "error" in res, url


async def test_fetch_url_html_and_redirect() -> None:
    from concierge.agent import tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redir":
            return httpx.Response(302, headers={"location": "/page"})
        if request.url.path == "/doc.pdf":
            return httpx.Response(200, content=b"%PDF-1.4 fake")
        return httpx.Response(
            200,
            text=(
                "<html><body><article><h1>T</h1>"
                "<p>Здравствуй, мир — текст тестовой страницы.</p>"
                "</article></body></html>"
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        # 93.184.216.34 is example.com — public IP, resolved without DNS.
        res = await tools._fetch_url(client, "http://93.184.216.34/redir")
        assert res["status"] == 200
        assert res["url"].endswith("/page")
        assert "Здравствуй" in res["text"]


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


async def test_deepseek_provider_kind(engine, monkeypatch) -> None:
    from pydantic_ai.models.openai import OpenAIChatModel

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name="p-deepseek", kind="deepseek", api_key_ref="DEEPSEEK_API_KEY"
        )
        session.add(provider)
        await session.flush()
        session.add(
            CapabilityBinding(
                capability="chat",
                provider_id=provider.id,
                model="deepseek-flash",
                params={},
            )
        )
        await session.commit()
    user = await make_user("ds", "adult")
    async with db.get_sessionmaker()() as session:
        bound = await get_model(session, "chat", user)
    assert bound is not None
    assert isinstance(bound.model, OpenAIChatModel)
    assert bound.model.model_name == "deepseek-flash"


async def test_base_url_suffix_normalized_at_use(engine) -> None:
    from concierge.agent.providers import http_base_url

    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name="p-ya",
            kind="openai-compatible",
            base_url="https://ai.api.cloud.yandex.net/v1/chat/completions",
        )
        session.add(provider)
        await session.commit()
        assert http_base_url(provider) == "https://ai.api.cloud.yandex.net/v1"
        provider.base_url = "http://x/v1/"
        assert http_base_url(provider) == "http://x/v1"


async def test_provider_api_key_literal_fallback(engine) -> None:
    """A raw key pasted into api_key_ref is used as the key itself."""
    from concierge.agent.providers import provider_api_key

    user = await make_user("rawkey", "adult")
    async with db.get_sessionmaker()() as session:
        provider = ModelProvider(
            name="p-raw", kind="openai-compatible", api_key_ref="AQVN-raw-key"
        )
        session.add(provider)
        await session.commit()
        assert (
            await provider_api_key(session, user, provider) == "AQVN-raw-key"
        )
        # env ref wins when it resolves
        provider.api_key_ref = "PATH"
        assert await provider_api_key(session, user, provider) != "PATH"


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
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool_call"}
    assert {"search_messages", "current_datetime"} <= tool_names


async def test_ask_model_permission_gate() -> None:
    from concierge.agent import tools

    allow_all = {"tools": {"allow": ["*"], "deny": []}}
    assert "ask_model" in tools.allowed_tool_names(allow_all)
    denied = {"tools": {"allow": ["*"], "deny": ["ask_model"]}}
    assert "ask_model" not in tools.allowed_tool_names(denied)


async def test_ask_model_tool_calls_named_model(engine) -> None:
    from types import SimpleNamespace

    from concierge.agent.tools import ToolDeps, make_ask_model_tool

    await _add_named_model("Алиса", params={"custom_output_text": "ответ Алисы"})
    user = await make_user("asker", "adult")
    tool = make_ask_model_tool(["Алиса"])
    ctx = SimpleNamespace(deps=ToolDeps(user_id=user.id, conversation_id=0))

    res = await tool(ctx, "Алиса", "какой ГОСТ за борщ?")  # type: ignore[arg-type]
    assert res == {"model": "Алиса", "answer": "ответ Алисы"}

    res = await tool(ctx, "лиля", "q")  # type: ignore[arg-type]
    assert "error" in res and res["available"] == ["Алиса"]


async def test_ask_model_named_tools(engine) -> None:
    from types import SimpleNamespace

    from concierge.agent.tools import ToolDeps, make_ask_model_tool

    await _add_named_model(
        "Алиса",
        params={
            "call_tools": ["current_datetime"],
            "custom_output_text": "ответ Алисы",
        },
        tools=["current_datetime"],
    )
    user = await make_user("asker_t", "adult")
    tool = make_ask_model_tool(["Алиса"])
    ctx = SimpleNamespace(deps=ToolDeps(user_id=user.id, conversation_id=0))

    # Granted tool is registered on the nested agent — the call succeeds.
    res = await tool(ctx, "Алиса", "q")  # type: ignore[arg-type]
    assert res == {"model": "Алиса", "answer": "ответ Алисы"}

    # Same model but the checklist is empty: the demanded tool isn't
    # registered and the nested run fails.
    await _add_named_model(
        "Лиля",
        params={"call_tools": ["current_datetime"]},
        tools=[],
    )
    res = await tool(ctx, "Лиля", "q")  # type: ignore[arg-type]
    assert "error" in res

    # The checklist can't exceed the caller's role permissions (guest
    # denies all tools).
    guest = await make_user("asker_guest", "guest")
    ctx = SimpleNamespace(deps=ToolDeps(user_id=guest.id, conversation_id=0))
    res = await tool(ctx, "Алиса", "q")  # type: ignore[arg-type]
    assert "error" in res


async def test_ask_model_exposed_in_run(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider(
        "chat",
        params={
            "custom_output_text": "ok",
            "call_tools": ["ask_model"],
        },
    )
    await _add_named_model("Алиса")
    await make_user("asker2", "adult")
    token = await login(client, "asker2")
    cid = await _make_conv(client, token)
    msg = await _post_and_collect(client, token, cid, "спроси Алису про борщ")

    run = await _run_and_wait(cid, msg["id"])
    assert run.status == "done"
    async with db.get_sessionmaker()() as session:
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool_call"}
    assert "ask_model" in tool_names


async def test_ask_model_hidden_without_named_models(
    engine, client: httpx.AsyncClient
) -> None:
    # Without named_models rows the tool isn't registered at all, so a
    # TestModel demanding it hits UnexpectedToolBehavior and the run fails.
    await _add_provider(
        "chat",
        params={
            "custom_output_text": "ok",
            "call_tools": ["ask_model"],
        },
    )
    await make_user("asker3", "adult")
    token = await login(client, "asker3")
    cid = await _make_conv(client, token)
    msg = await _post_and_collect(client, token, cid, "спроси Алису")

    run = await _run_and_wait(cid, msg["id"])
    assert run.status == "failed"


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
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool_call"}
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
    tool_names = {p["name"] for p in reply.parts if p["type"] == "tool_call"}
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
    tool_names_k = {p["name"] for p in reply_k.parts if p["type"] == "tool_call"}
    assert "echo_srv_echo" not in tool_names_k


async def test_reply_mode_mention(engine, client: httpx.AsyncClient) -> None:
    await _add_provider("chat", params={"custom_output_text": "ok"})
    await make_user("own", "adult")
    member = await make_user("mem", "adult")
    token = await login(client, "own")
    resp = await client.post(
        "/conversations",
        json={"kind": "group", "title": "g", "member_ids": [member.id]},
        headers=auth(token),
    )
    cid = int(resp.json()["id"])
    assert resp.json()["reply_mode"] == "mention"
    assert resp.json()["assistant_name"] == "Консьерж"

    resp = await client.put(
        "/settings/user",
        json={"key": "agent.name", "value": "Джарвис"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    conv = await client.get(f"/conversations/{cid}", headers=auth(token))
    assert conv.json()["assistant_name"] == "Джарвис"

    await _post_and_collect(client, token, cid, "просто чат")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        runs = (
            await session.execute(
                select(Run).where(Run.conversation_id == cid)
            )
        ).scalars()
        assert list(runs) == []

    # A mention of another member is not an address to the concierge.
    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={
            "parts": [
                {"type": "text", "text": "@Mem привет"},
                {"type": "mention", "user_id": member.id, "name": "Mem"},
            ]
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        runs = (
            await session.execute(
                select(Run).where(Run.conversation_id == cid)
            )
        ).scalars()
        assert list(runs) == []

    # An assistant-tagged mention part triggers a run.
    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={
            "parts": [
                {"type": "text", "text": "привет"},
                {"type": "mention", "assistant": True},
            ]
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    run = await _run_and_wait(cid, int(resp.json()["id"]))
    assert run.status == "done"

    msg_yes = await _post_and_collect(client, token, cid, "@консьерж привет")
    run = await _run_and_wait(cid, msg_yes["id"])
    assert run.status == "done"

    # Typed text without "@" stays silent, but a transcribed phrase naming
    # the assistant triggers (voice transcripts carry no "@").
    await _post_and_collect(client, token, cid, "консьерж, привет")
    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={
            "parts": [
                {
                    "type": "text",
                    "text": "просто разговор без обращения",
                    "transcribed": True,
                }
            ]
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        runs = (
            await session.execute(
                select(func.count()).select_from(Run).where(
                    Run.conversation_id == cid
                )
            )
        ).scalar_one()
    assert runs == 2

    resp = await client.post(
        f"/conversations/{cid}/messages",
        json={
            "parts": [
                {
                    "type": "text",
                    "text": "Консьерж, включи проектор",
                    "transcribed": True,
                }
            ]
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    run = await _run_and_wait(cid, int(resp.json()["id"]))
    assert run.status == "done"


async def test_stream_deltas_include_first_chunk(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """The first text chunk arrives as PartStartEvent, not PartDeltaEvent —
    it must reach message.delta too (else the reply starts one chunk late)."""
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.settings import ModelSettings

    from concierge.agent import providers
    from concierge.agent.providers import BoundModel
    from concierge.channels import ChannelEvent, get_hub

    async def scripted_stream(messages, agent_info):
        yield "Па"
        yield "риж"

    real_get_model = providers.get_model

    async def fake_get_model(session, capability, user):
        if capability == "chat":
            return BoundModel(
                FunctionModel(stream_function=scripted_stream),
                "test/scripted",
                ModelSettings(),
            )
        return await real_get_model(session, capability, user)

    monkeypatch.setattr(providers, "get_model", fake_get_model)

    await make_user("streamy", "adult")
    token = await login(client, "streamy")
    cid = await _make_conv(client, token)

    deltas: list[str] = []

    class RecSink:
        presence = False
        user_id = 0

        async def emit(self, event: ChannelEvent) -> None:
            if event.type == "message.delta":
                deltas.append(str(event.data.get("text", "")))

    conn = get_hub().connect(RecSink())
    await get_hub().subscribe(conn, cid)
    try:
        msg = await _post_and_collect(client, token, cid, "столица Франции")
        await get_runtime().wait_idle()
    finally:
        await get_hub().disconnect(conn)

    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(
                select(Run).where(Run.trigger_message_id == msg["id"])
            )
        ).scalar_one()
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    final = "".join(str(p["text"]) for p in reply.parts if p["type"] == "text")
    assert final == "Париж"
    assert "".join(deltas) == final


async def test_stream_thinking_deltas(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """Thinking parts stream as message.thinking alongside message.delta —
    a new ThinkingPart emits start=true, continuation deltas don't, and
    signature-only deltas carry no text (never reach the client)."""
    from pydantic_ai.models.function import (
        DeltaThinkingPart,
        FunctionModel,
    )
    from pydantic_ai.settings import ModelSettings

    from concierge.agent import providers
    from concierge.agent.providers import BoundModel
    from concierge.channels import ChannelEvent, get_hub

    async def scripted_stream(messages, agent_info):
        yield {0: DeltaThinkingPart(content="хм")}
        yield {0: DeltaThinkingPart(content=", думаю")}
        yield {0: DeltaThinkingPart(signature="sig-1")}
        yield "Па"
        yield "риж"
        yield {1: DeltaThinkingPart(content="ещё")}

    real_get_model = providers.get_model

    async def fake_get_model(session, capability, user):
        if capability == "chat":
            return BoundModel(
                FunctionModel(stream_function=scripted_stream),
                "test/scripted",
                ModelSettings(),
            )
        return await real_get_model(session, capability, user)

    monkeypatch.setattr(providers, "get_model", fake_get_model)

    await make_user("thinky", "adult")
    token = await login(client, "thinky")
    cid = await _make_conv(client, token)

    deltas: list[str] = []
    thinking: list[tuple[str, bool]] = []

    class RecSink:
        presence = False
        user_id = 0

        async def emit(self, event: ChannelEvent) -> None:
            if event.type == "message.delta":
                deltas.append(str(event.data.get("text", "")))
            elif event.type == "message.thinking":
                thinking.append(
                    (
                        str(event.data.get("text", "")),
                        event.data.get("start") is True,
                    )
                )

    conn = get_hub().connect(RecSink())
    await get_hub().subscribe(conn, cid)
    try:
        msg = await _post_and_collect(client, token, cid, "столица Франции")
        await get_runtime().wait_idle()
    finally:
        await get_hub().disconnect(conn)

    # Thinking went to its own event and never leaked into message.delta.
    assert "".join(deltas) == "Париж"
    assert thinking == [
        ("хм", True),
        (", думаю", False),
        ("ещё", True),
    ]

    async with db.get_sessionmaker()() as session:
        run = (
            await session.execute(
                select(Run).where(Run.trigger_message_id == msg["id"])
            )
        ).scalar_one()
        reply = (
            await session.execute(
                select(Message).where(Message.run_id == run.id)
            )
        ).scalar_one()
    thought = [p for p in reply.parts if p["type"] == "thinking"]
    assert [str(p.get("content", "")) for p in thought] == [
        "хм, думаю",
        "ещё",
    ]
    assert thought[0].get("signature") == "sig-1"


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


async def test_run_persists_tool_calls_and_compacts(
    engine, client: httpx.AsyncClient
) -> None:
    """Tool calls/returns are stored on the reply message, and once estimated
    context passes 75% of binding params.context_length a compaction marker
    message is posted and future history starts from its summary."""
    await _add_provider(
        "chat",
        params={"custom_output_text": "ответ", "context_length": 80},
    )
    await _add_provider(
        "chat_fast", params={"custom_output_text": "конспект"}
    )
    user = await make_user("carol", "adult")
    token = await login(client, "carol")
    cid = await _make_conv(client, token)
    last = None
    for i in range(9):
        last = await _post_and_collect(
            client, token, cid, f"сообщение {i} " + "x" * 60
        )
        await _run_and_wait(cid, last["id"])

    async with db.get_sessionmaker()() as session:
        msgs = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == cid)
                    .order_by(Message.seq)
                )
            ).scalars()
        )
        conv = await session.get(Conversation, cid)
    replies = [m for m in msgs if m.author_kind == "assistant"]
    assert replies
    # TestModel calls tools on the first run only
    calls = [p for p in replies[0].parts if p["type"] == "tool_call"]
    rets = [p for p in replies[0].parts if p["type"] == "tool_return"]
    assert calls and "args" in calls[0] and calls[0]["tool_call_id"]
    assert rets and "content" in rets[0]
    texts = [p for p in replies[0].parts if p["type"] == "text"]
    assert texts and texts[-1]["text"] == "ответ"

    markers = [
        m
        for m in msgs
        if m.author_kind == "system"
        and any(p.get("type") == "compaction" for p in m.parts)
    ]
    assert markers, "expected a compaction marker message"
    assert conv is not None
    hist = get_runtime()._build_history(msgs, conv, {user.id: "Carol"}, {})
    assert isinstance(hist[0], ModelRequest)
    assert isinstance(hist[0].parts[0], UserPromptPart)
    assert "Контекст сжат автоматически" in str(hist[0].parts[0].content)


async def test_invite_member_tool(engine) -> None:
    from types import SimpleNamespace

    from concierge.agent.tools import ToolDeps, invite_member
    from concierge.conversations import service as conv_service
    from concierge.conversations.models import ConversationMember

    alice = await make_user("alice_i", "adult")
    tikhon = await make_user("tikhon", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conv_service.create_conversation(
            session, alice, "personal", "", []
        )
        assert conv is not None
        conv_id = conv.id
    ctx = SimpleNamespace(
        deps=ToolDeps(user_id=alice.id, conversation_id=conv_id)
    )

    res = await invite_member(ctx, "Tikhon")  # type: ignore[arg-type]
    assert res == {
        "ok": True,
        "user": tikhon.display_name,
        "conversation_id": conv_id,
    }
    async with db.get_sessionmaker()() as session:
        member = await session.get(ConversationMember, (conv_id, tikhon.id))
        assert member is not None
        conv = await session.get(Conversation, conv_id)
        assert conv is not None
        assert conv.kind == "group"
        notice = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id,
                    Message.author_kind == "system",
                )
            )
        ).scalar_one()
    assert tikhon.display_name in str(notice.parts[0]["text"])

    res = await invite_member(ctx, "Tikhon")  # type: ignore[arg-type]
    assert res.get("already_member") is True

    res = await invite_member(ctx, "Nobody")  # type: ignore[arg-type]
    assert "error" in res and res["candidates"] == []


async def test_resolve_user_ambiguous(engine) -> None:
    from concierge.agent.tools import _resolve_user

    await make_user("nastya_a", "adult")
    await make_user("nastya_b", "adult")
    async with db.get_sessionmaker()() as session:
        user, candidates = await _resolve_user(session, "nastya")
        assert user is None
        assert len(candidates) == 2
        user, candidates = await _resolve_user(session, "nastya_a")
        assert user is not None and user.username == "nastya_a"


async def test_forward_messages_tool(engine) -> None:
    from types import SimpleNamespace

    from concierge.agent.tools import ToolDeps, forward_messages
    from concierge.conversations import service as conv_service

    alice = await make_user("alice_f", "adult")
    await make_user("nastya", "adult")
    async with db.get_sessionmaker()() as session:
        conv = await conv_service.create_conversation(
            session, alice, "personal", "", []
        )
        assert conv is not None
        conv_id = conv.id
        await conv_service.post_message(
            session, conv, alice, [{"type": "text", "text": "первое"}], None
        )
        await conv_service.post_message(
            session, conv, alice, [{"type": "text", "text": "второе"}], None
        )
        trigger, _ = await conv_service.post_message(
            session,
            conv,
            alice,
            [{"type": "text", "text": "перешли это Насте"}],
            None,
        )
        trigger_seq = trigger.seq
    ctx = SimpleNamespace(
        deps=ToolDeps(
            user_id=alice.id,
            conversation_id=conv_id,
            trigger_seq=trigger_seq,
        )
    )

    res = await forward_messages(ctx, "Nastya")  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["forwarded"] == 1
    target_id = res["conversation_id"]
    assert target_id != conv_id

    async with db.get_sessionmaker()() as session:
        target = await session.get(Conversation, target_id)
        assert target is not None
        assert target.kind == "group"
        nastya_id = (
            await session.execute(
                select(User.id).where(User.username == "nastya")
            )
        ).scalar_one()
        assert {m.user_id for m in target.members} == {alice.id, nastya_id}
        msgs = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == target_id)
                    .order_by(Message.seq)
                )
            ).scalars()
        )
    assert len(msgs) == 1
    assert msgs[0].author_id == alice.id
    part = msgs[0].parts[0]
    assert part["type"] == "text" and part["forwarded"] is True
    assert "Переслано от" in part["text"] and "второе" in part["text"]

    # A second forward lands in the same direct session.
    res = await forward_messages(ctx, "Nastya", count=2)  # type: ignore[arg-type]
    assert res["conversation_id"] == target_id
    assert res["forwarded"] == 2
    async with db.get_sessionmaker()() as session:
        n = (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == target_id
                )
            )
        ).scalar_one()
    assert n == 3

    # Recipient already in the conversation -> no copy is made.
    async with db.get_sessionmaker()() as session:
        await conv_service.add_members(
            session, await session.get(Conversation, conv_id), [nastya_id]  # type: ignore[arg-type]
        )
    res = await forward_messages(ctx, "Nastya")  # type: ignore[arg-type]
    assert res.get("already_member") is True


async def test_forward_messages_gap_tail(engine) -> None:
    from datetime import timedelta as _td
    from types import SimpleNamespace

    from concierge.agent.tools import ToolDeps, forward_messages
    from concierge.conversations import service as conv_service

    alice = await make_user("alice_g", "adult")
    await make_user("tikhon_g", "adult")
    base = datetime(2026, 1, 1, 12, 0, 0)
    async with db.get_sessionmaker()() as session:
        conv = await conv_service.create_conversation(
            session, alice, "personal", "", []
        )
        assert conv is not None
        conv_id = conv.id
        stamps = [
            base,  # old
            base + _td(hours=1),  # +60min gap
            base + _td(hours=1, minutes=10),
            base + _td(hours=1, minutes=20),
        ]
        for i, ts in enumerate(stamps):
            msg, _ = await conv_service.post_message(
                session,
                conv,
                alice,
                [{"type": "text", "text": f"msg{i}"}],
                None,
            )
            msg.created_at = ts
            await session.commit()
        trigger, _ = await conv_service.post_message(
            session,
            conv,
            alice,
            [{"type": "text", "text": "перешли последние Тихону"}],
            None,
        )
        trigger_seq = trigger.seq
    ctx = SimpleNamespace(
        deps=ToolDeps(
            user_id=alice.id,
            conversation_id=conv_id,
            trigger_seq=trigger_seq,
        )
    )
    # Pauses above 30 min cut the tail: only the last three messages go.
    res = await forward_messages(  # type: ignore[arg-type]
        ctx, "Tikhon_g", gap_minutes=30
    )
    assert res["ok"] is True and res["forwarded"] == 3
    async with db.get_sessionmaker()() as session:
        texts = [
            str(p["text"])
            for m in (
                (
                    await session.execute(
                        select(Message).where(
                            Message.conversation_id
                            == res["conversation_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            for p in m.parts
            if p["type"] == "text"
        ]
    assert not any("msg0" in t for t in texts)
    assert all(any(f"msg{i}" in t for t in texts) for i in (1, 2, 3))
