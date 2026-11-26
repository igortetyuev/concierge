"""Skills inside the agent runtime: activation, tools, choices, subagents."""

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import (
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.settings import ModelSettings
from sqlalchemy import select

from concierge.agent import providers
from concierge.agent.providers import BoundModel
from concierge.agent.runtime import get_runtime
from concierge.agent.tools import ToolDeps
from concierge.conversations.models import Conversation, Message, Run
from concierge.core import db
from concierge.skills import service as skill_service
from concierge.skills.models import Skill
from tests.conftest import auth, login, make_user
from tests.test_agent import _add_provider, _make_conv, _post_and_collect

SKILL_MD = """---
name: shopping
description: Помогает составить список покупок
aliases: [покупки]
triggers:
  - список покупок
argument-hint: "<магазин>"
---

# Покупки
Спроси про магазин: $ARGUMENTS.
"""

SKILL_MD_TRIGGER = """---
name: notes
description: Заметки
triggers:
  - веди заметки
---

# Заметки
Тело.
"""

SKILL_MD_EPHEMERAL = """---
name: school
description: Школьная справка
ephemeral: true
aliases: [школа]
triggers:
  - домашка
---

# Школа
Таблицы: school_grades.
"""


def _text_fn(out: str = "ок"):
    async def sf(messages: Any, agent_info: Any):
        yield out

    return sf


async def _use_function_model(monkeypatch, sf) -> list[Any]:
    """Bind `chat` to a FunctionModel; returns the captured AgentInfos."""
    import json as _json

    infos: list[Any] = []

    async def wrap(messages: Any, agent_info: Any):
        infos.append(agent_info)
        async for item in sf(messages, agent_info):
            yield item

    async def wrap_plain(messages: Any, agent_info: Any) -> ModelResponse:
        # Non-streamed requests (run_subagent) collect the same script.
        parts: list[Any] = []
        texts: list[str] = []
        async for item in wrap(messages, agent_info):
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                for d in item.values():
                    parts.append(
                        ToolCallPart(
                            tool_name=d.name,
                            args=_json.loads(d.json_args or "{}"),
                            tool_call_id=d.tool_call_id or "",
                        )
                    )
        if texts or not parts:
            parts.append(TextPart("".join(texts)))
        return ModelResponse(parts=parts)

    real = providers.get_model

    async def fake(session, capability, user):
        if capability == "chat":
            return BoundModel(
                FunctionModel(wrap_plain, stream_function=wrap),
                "test/function",
                ModelSettings(),
            )
        return await real(session, capability, user)

    monkeypatch.setattr(providers, "get_model", fake)
    return infos


async def _add_skill(user_id: int | None, content: str = SKILL_MD) -> Skill:
    async with db.get_sessionmaker()() as session:
        return await skill_service.create_skill(
            session, user_id, content, created_by=user_id or 1
        )


async def _runs(conv_id: int) -> list[Run]:
    async with db.get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(Run).where(Run.conversation_id == conv_id)
                )
            ).scalars()
        )


async def _messages(conv_id: int) -> list[Message]:
    async with db.get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conv_id)
                    .order_by(Message.seq)
                )
            ).scalars()
        )


async def test_slash_command_activates_and_persists(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    infos = await _use_function_model(monkeypatch, _text_fn("готово"))
    user = await make_user("slash", "adult")
    await _add_skill(user.id)
    token = await login(client, "slash")
    cid = await _make_conv(client, token)

    await _post_and_collect(client, token, cid, "/shopping ашан")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        state = conv.settings["active_skill"]
        assert state["name"] == "shopping" and state["args"] == "ашан"
    msgs = await _messages(cid)
    event = next(
        m for m in msgs for p in m.parts if p.get("type") == "skill_event"
    )
    assert event.parts[0]["action"] == "start"
    instr = infos[-1].instructions
    assert "Спроси про магазин: ашан." in instr
    assert "Активный скилл: shopping" in instr
    assert "ask_user_question" in instr  # compat preamble

    # A follow-up plain message still gets the skill body injected.
    await _post_and_collect(client, token, cid, "продолжим")
    await get_runtime().wait_idle()
    assert "Активный скилл: shopping" in infos[-1].instructions

    # /stop ends it without a run.
    n_runs = len(await _runs(cid))
    await _post_and_collect(client, token, cid, "/stop")
    await get_runtime().wait_idle()
    assert len(await _runs(cid)) == n_runs
    msgs = await _messages(cid)
    ends = [
        p
        for m in msgs
        for p in m.parts
        if p.get("type") == "skill_event" and p.get("action") == "end"
    ]
    assert ends
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})


async def test_trigger_phrase_and_alias(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await _use_function_model(monkeypatch, _text_fn("ок"))
    user = await make_user("trig", "adult")
    await _add_skill(user.id, SKILL_MD_TRIGGER)
    token = await login(client, "trig")
    cid = await _make_conv(client, token)

    await _post_and_collect(client, token, cid, "Веди заметки, пожалуйста")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert conv.settings["active_skill"]["name"] == "notes"
        assert conv.settings["active_skill"]["args"] == ""

    # Alias /команда switches the active skill.
    await _add_skill(user.id, SKILL_MD)
    await _post_and_collect(client, token, cid, "/покупки")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert conv.settings["active_skill"]["name"] == "shopping"


async def test_trigger_phrase_only_without_active(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await _use_function_model(monkeypatch, _text_fn("ок"))
    user = await make_user("trig2", "adult")
    await _add_skill(user.id)
    await _add_skill(user.id, SKILL_MD_TRIGGER)
    token = await login(client, "trig2")
    cid = await _make_conv(client, token)

    # Activate one skill explicitly; the other's phrase must not switch it.
    await _post_and_collect(client, token, cid, "/shopping")
    await get_runtime().wait_idle()
    await _post_and_collect(client, token, cid, "веди заметки")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert conv.settings["active_skill"]["name"] == "shopping"


async def test_ephemeral_slash_command(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    infos = await _use_function_model(monkeypatch, _text_fn("ок"))
    user = await make_user("eph", "adult")
    await _add_skill(user.id, SKILL_MD_EPHEMERAL)
    token = await login(client, "eph")
    cid = await _make_conv(client, token)

    await _post_and_collect(client, token, cid, "/школа")
    await get_runtime().wait_idle()
    assert "Таблицы: school_grades." in infos[-1].instructions
    assert "Скилл: school — на этот ответ" in infos[-1].instructions
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})
    msgs = await _messages(cid)
    assert not [
        p for m in msgs for p in m.parts if p.get("type") == "skill_event"
    ]

    # Nothing stays pinned: the next message gets no skill chunk.
    await _post_and_collect(client, token, cid, "продолжим")
    await get_runtime().wait_idle()
    assert "school_grades" not in infos[-1].instructions


async def test_ephemeral_trigger_phrase(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    infos = await _use_function_model(monkeypatch, _text_fn("ок"))
    user = await make_user("epht", "adult")
    await _add_skill(user.id, SKILL_MD_EPHEMERAL)
    token = await login(client, "epht")
    cid = await _make_conv(client, token)

    await _post_and_collect(client, token, cid, "какая домашка на завтра")
    await get_runtime().wait_idle()
    assert "Таблицы: school_grades." in infos[-1].instructions
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})
    msgs = await _messages(cid)
    assert not [
        p for m in msgs for p in m.parts if p.get("type") == "skill_event"
    ]


async def test_ephemeral_use_skill_tool(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider(
        "chat",
        params={"call_tools": ["use_skill"], "custom_output_text": "ок"},
    )
    user = await make_user("ephtool", "adult")
    await _add_skill(user.id, SKILL_MD_EPHEMERAL)
    token = await login(client, "ephtool")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "запусти скилл")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})
    msgs = await _messages(cid)
    assert not [
        p for m in msgs for p in m.parts if p.get("type") == "skill_event"
    ]


async def test_unknown_command_is_plain_text(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await _use_function_model(monkeypatch, _text_fn("ок"))
    await make_user("unk", "adult")
    token = await login(client, "unk")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "/nosuchskill привет")
    await get_runtime().wait_idle()
    runs = await _runs(cid)
    assert len(runs) == 1 and runs[0].status == "done"
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})


async def test_skill_command_wakes_mention_mode(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await _use_function_model(monkeypatch, _text_fn("ок"))
    await make_user("gowner", "adult")
    member = await make_user("gmem", "adult")
    await _add_skill(member.id)
    token = await login(client, "gowner")
    resp = await client.post(
        "/conversations",
        json={"kind": "group", "title": "g", "member_ids": [member.id]},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    mtoken = await login(client, "gmem")
    await _post_and_collect(client, mtoken, cid, "/покупки")
    await get_runtime().wait_idle()
    runs = await _runs(cid)
    assert len(runs) == 1 and runs[0].status == "done"
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert conv.settings["active_skill"]["name"] == "shopping"


async def test_deleted_skill_silently_deactivates(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    await _use_function_model(monkeypatch, _text_fn("ок"))
    user = await make_user("del", "adult")
    skill = await _add_skill(user.id)
    token = await login(client, "del")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "/shopping")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        s = await session.get(Skill, skill.id)
        assert s is not None
        await session.delete(s)
        await session.commit()
    await _post_and_collect(client, token, cid, "продолжим")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})


async def test_stop_not_lost_during_busy_run(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """A /stop sent while the run holds the lock is requeued, not dropped."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(messages: Any, agent_info: Any):
        started.set()
        await gate.wait()
        yield "ок"

    await _use_function_model(monkeypatch, slow)
    user = await make_user("busy", "adult")
    await _add_skill(user.id)
    token = await login(client, "busy")
    cid = await _make_conv(client, token)

    await _post_and_collect(client, token, cid, "/shopping")
    await _post_and_collect(client, token, cid, "долгий вопрос")
    # The busy run is parked inside the model call (its DB commits are done),
    # so the /stop probe below can't interleave a cursor on the shared
    # sqlite connection.
    await asyncio.wait_for(started.wait(), timeout=5)
    await _post_and_collect(client, token, cid, "/stop")
    gate.set()
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert "active_skill" not in (conv.settings or {})


# ---------- tools ----------


async def test_use_skill_tool_activates(
    engine, client: httpx.AsyncClient
) -> None:
    await _add_provider(
        "chat",
        params={"call_tools": ["use_skill"], "custom_output_text": "ок"},
    )
    user = await make_user("tooluser", "adult")
    await _add_skill(user.id)
    token = await login(client, "tooluser")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "запусти скилл")
    await get_runtime().wait_idle()
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        assert conv.settings["active_skill"]["name"] == "shopping"
        calls = (
            await session.execute(
                select(Message).where(Message.conversation_id == cid)
            )
        ).scalars().all()
    events = [
        p for m in calls for p in m.parts if p.get("type") == "skill_event"
    ]
    assert events and events[0]["action"] == "start"


async def test_use_skill_records_skill_on_deps(engine) -> None:
    """use_skill marks deps so interactive parts it produces carry the
    skill (ephemeral ones have no pinned state to recover it from)."""
    from concierge.conversations import service as conv_service
    from concierge.skills.tools import make_use_skill_tool

    user = await make_user("usdep", "adult")
    skill = await _add_skill(user.id, SKILL_MD_EPHEMERAL)
    async with db.get_sessionmaker()() as session:
        conv = await conv_service.create_conversation(
            session, user, "personal", "", []
        )
        assert conv is not None
        cid = conv.id
    deps = ToolDeps(user_id=user.id, conversation_id=cid)
    tool = make_use_skill_tool(["school"])
    res = await tool(SimpleNamespace(deps=deps), name="school")  # type: ignore[arg-type]
    assert res["ok"] is True and res["ephemeral"] is True
    assert deps.skill_name == "school" and deps.skill_id == skill.id


async def test_read_skill_file_tool(engine) -> None:
    from concierge.skills.tools import read_skill_file

    user = await make_user("reader", "adult")
    async with db.get_sessionmaker()() as session:
        skill = await skill_service.create_skill(
            session, user.id, SKILL_MD, created_by=user.id
        )
        await skill_service.put_file(
            session, skill, "references/units.md", "единицы: шт"
        )
        from concierge.conversations import service as conv_service

        conv = await conv_service.create_conversation(
            session, user, "personal", "", []
        )
        assert conv is not None
        conv_id = conv.id
    ctx = SimpleNamespace(
        deps=ToolDeps(user_id=user.id, conversation_id=conv_id)
    )
    res = await read_skill_file(ctx, "references/units.md")  # type: ignore[arg-type]
    assert "error" in res  # no active skill yet
    async with db.get_sessionmaker()() as session:
        conv = await session.get(Conversation, conv_id)
        skill = await session.get(Skill, skill.id)
        assert conv is not None and skill is not None
        await skill_service.activate(session, conv, skill, "", user)
    res = await read_skill_file(ctx, "references/units.md")  # type: ignore[arg-type]
    assert res["content"] == "единицы: шт"
    res = await read_skill_file(ctx, "../etc/passwd")  # type: ignore[arg-type]
    assert "error" in res


async def test_save_skill_permission_and_call(engine) -> None:
    from concierge.agent import tools
    from concierge.skills.tools import save_skill

    allow_all = {"tools": {"allow": ["*"], "deny": []}}
    assert "save_skill" not in tools.allowed_tool_names(allow_all)
    with_perm = {
        "tools": {"allow": ["*"], "deny": []},
        "create_skills": True,
    }
    assert "save_skill" in tools.allowed_tool_names(with_perm)

    for name in (
        "use_skill",
        "end_skill",
        "read_skill_file",
        "save_skill",
        "ask_user_question",
        "show_checklist",
        "run_subagent",
    ):
        assert name not in tools.NAMED_MODEL_TOOLS

    user = await make_user("saver", "adult")
    ctx = SimpleNamespace(
        deps=ToolDeps(user_id=user.id, conversation_id=0)
    )
    res = await save_skill(ctx, SKILL_MD)  # type: ignore[arg-type]
    assert res["ok"] is True and res["skill"] == "shopping"
    async with db.get_sessionmaker()() as session:
        skills = await skill_service.personal_skills(session, user.id)
        assert [s.name for s in skills] == ["shopping"]


# ---------- ask_user_question / answer ----------


async def test_choice_flow(engine, client: httpx.AsyncClient, monkeypatch) -> None:
    calls = {"n": 0}
    questions = [
        {
            "question": "Какой магазин?",
            "header": "",
            "options": [
                {"label": "Ашан", "description": "большой"},
                {"label": "Пятёрочка", "description": ""},
            ],
            "multi_select": False,
        }
    ]

    import json as _json

    async def fn(messages: Any, agent_info: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="ask_user_question",
                        json_args=_json.dumps({"questions": questions}),
                        tool_call_id="c1",
                    )
                }
            )
        else:
            yield "принято"

    await _use_function_model(monkeypatch, fn)
    await make_user("ask", "adult")
    token = await login(client, "ask")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "спроси меня")
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    reply = msgs[-1]
    assert reply.author_kind == "assistant"
    choice = next(p for p in reply.parts if p["type"] == "choice")
    assert choice["answered"] is False and choice["questions"] == questions
    assert choice["skill"] is None  # no skill driving this run
    choice_id = choice["choice_id"]

    resp = await client.post(
        f"/conversations/{cid}/messages/{reply.id}/answer",
        json={
            "choice_id": choice_id,
            "answers": [{"selected": ["Ашан"], "other": None}],
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    user_answer = next(
        m for m in msgs if any(p.get("choice_answer") for p in m.parts)
    )
    assert "Ашан" in user_answer.parts[0]["text"]
    orig = next(m for m in msgs if m.id == reply.id)
    answered = next(p for p in orig.parts if p["type"] == "choice")
    assert answered["answered"] is True
    # The answer scheduled a follow-up run.
    assert len(await _runs(cid)) == 2

    # Repeat answer -> 409.
    resp = await client.post(
        f"/conversations/{cid}/messages/{reply.id}/answer",
        json={
            "choice_id": choice_id,
            "answers": [{"selected": ["Пятёрочка"], "other": None}],
        },
        headers=auth(token),
    )
    assert resp.status_code == 409


async def test_choice_answer_reactivates_ephemeral_and_wakes_mention(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """A choice asked under an ephemeral skill records it; answering
    re-injects the skill for the follow-up run and wakes a mention group."""
    questions = [
        {
            "question": "Подтвердить?",
            "options": [{"label": "да"}, {"label": "нет"}],
        }
    ]

    import json as _json

    calls = {"n": 0}

    async def fn(messages: Any, agent_info: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="ask_user_question",
                        json_args=_json.dumps({"questions": questions}),
                        tool_call_id="c1",
                    )
                }
            )
        else:
            yield "принято"

    infos = await _use_function_model(monkeypatch, fn)
    await make_user("cqown", "adult")
    member = await make_user("cqmem", "adult")
    skill = await _add_skill(member.id, SKILL_MD_EPHEMERAL)
    token = await login(client, "cqown")
    resp = await client.post(
        "/conversations",
        json={"kind": "group", "title": "g", "member_ids": [member.id]},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    mtoken = await login(client, "cqmem")

    # Group reply_mode is 'mention': the /command run is allowed through.
    await _post_and_collect(client, mtoken, cid, "/школа")
    await get_runtime().wait_idle()
    msgs = await _messages(cid)
    reply = msgs[-1]
    choice = next(p for p in reply.parts if p["type"] == "choice")
    assert choice["skill"] == "school" and choice["skill_id"] == skill.id

    resp = await client.post(
        f"/conversations/{cid}/messages/{reply.id}/answer",
        json={
            "choice_id": choice["choice_id"],
            "answers": [{"selected": ["да"], "other": None}],
        },
        headers=auth(mtoken),
    )
    assert resp.status_code == 201, resp.text
    await get_runtime().wait_idle()

    # The answer alone (no mention) produced a second run that re-injected
    # the ephemeral skill's instructions.
    assert len(await _runs(cid)) == 2
    assert "school_grades" in infos[-1].instructions
    msgs = await _messages(cid)
    answer = next(
        m for m in msgs if any(p.get("choice_answer") for p in m.parts)
    )
    snap = answer.parts[0]["choice_answer"]
    assert snap["skill"] == "school" and snap["skill_id"] == skill.id


async def test_ask_user_question_twice_and_history(engine) -> None:
    from concierge.agent import history as agent_history
    from concierge.agent.tools import AskQuestion, ask_user_question

    deps = ToolDeps(user_id=1, conversation_id=0)
    ctx = SimpleNamespace(deps=deps)
    q = [{"question": "Вопрос?"}]
    q_models = [AskQuestion(question="Вопрос?")]
    res = await ask_user_question(ctx, q_models)  # type: ignore[arg-type]
    assert res["ok"] is True
    res = await ask_user_question(ctx, q_models)  # type: ignore[arg-type]
    assert "error" in res

    # A stored choice part survives history replay (skipped, not crashing).
    msgs = agent_history.deserialize_parts(
        [
            {"type": "text", "text": "вопрос"},
            {
                "type": "choice",
                "choice_id": "x",
                "questions": q,
                "answered": True,
            },
        ]
    )
    assert msgs  # text part survived; unknown part skipped
    rendered = agent_history.render_for_summary(
        [{"type": "choice", "questions": q, "answered": True}]
    )
    assert "Вопрос?" in rendered


# ---------- run_subagent ----------


async def test_run_subagent(engine, client: httpx.AsyncClient, monkeypatch) -> None:
    from concierge.agent.tools import SUBAGENT_EXCLUDED

    child_tools: list[str] = []
    calls = {"parent": 0}

    async def fn(messages: Any, agent_info: Any):
        instructions = agent_info.instructions or ""
        if "субагент" in instructions:
            child_tools.extend(t.name for t in agent_info.function_tools)
            yield "результат субагента"
            return
        calls["parent"] += 1
        if calls["parent"] == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="run_subagent",
                        json_args='{"goal": "задача 1"}',
                        tool_call_id="s1",
                    ),
                    1: DeltaToolCall(
                        name="run_subagent",
                        json_args='{"goal": "задача 2"}',
                        tool_call_id="s2",
                    ),
                }
            )
        else:
            yield "готово"

    await _use_function_model(monkeypatch, fn)
    await make_user("parent", "adult")
    token = await login(client, "parent")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "сделай две задачи")
    await get_runtime().wait_idle()

    runs = await _runs(cid)
    assert len(runs) == 1 and runs[0].status == "done"
    msgs = await _messages(cid)
    reply = msgs[-1]
    sub_calls = [
        p
        for p in reply.parts
        if p["type"] == "tool_call" and p["name"] == "run_subagent"
    ]
    rets = [
        p
        for p in reply.parts
        if p["type"] == "tool_return" and p["name"] == "run_subagent"
    ]
    assert len(sub_calls) == 2  # parallel batch returned
    assert len(rets) == 2
    assert all(
        "результат субагента" in str(r["content"]) for r in rets
    )
    # The child agent's toolset has no excluded names.
    assert child_tools
    assert not set(child_tools) & SUBAGENT_EXCLUDED
    assert "invite_member" not in child_tools
    # usage of the parent run includes the subagent's requests.
    usage = runs[0].usage
    assert usage.get("requests", 0) >= 3  # parent x2 + child x2? at least 3


async def test_subagent_depth_guard(engine) -> None:
    from concierge.agent.tools import run_subagent

    user = await make_user("deep", "adult")
    ctx = SimpleNamespace(
        deps=ToolDeps(
            user_id=user.id, conversation_id=0, subagent_depth=1
        )
    )
    res = await run_subagent(ctx, "g")  # type: ignore[arg-type]
    assert "error" in res
