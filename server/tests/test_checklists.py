"""Checklists: show_checklist tool, /checklist submit endpoint, runtime."""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.settings import ModelSettings

from concierge.agent import providers
from concierge.agent.providers import BoundModel
from concierge.agent.runtime import get_runtime
from concierge.agent.tools import ToolDeps
from tests.conftest import auth, login, make_user
from tests.test_agent import _make_conv, _post_and_collect
from tests.test_skill_runtime import (
    SKILL_MD_EPHEMERAL,
    _add_skill,
    _messages,
    _runs,
)


async def _bind_fn(monkeypatch, sf) -> tuple[list[Any], list[Any]]:
    """Bind `chat` to a FunctionModel; returns (agent_infos, requests)."""
    infos: list[Any] = []
    requests: list[Any] = []

    async def wrap(messages: Any, agent_info: Any):
        infos.append(agent_info)
        requests.append(messages)
        async for item in sf(messages, agent_info):
            yield item

    async def wrap_plain(messages: Any, agent_info: Any) -> ModelResponse:
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
                            args=json.loads(d.json_args or "{}"),
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
    return infos, requests


def _prompt_texts(messages: list[Any]) -> list[str]:
    out: list[str] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    out.append(str(p.content))
    return out


def _cl_fn(calls: dict[str, int], **tool_args: Any):
    """First model call shows a checklist; later calls answer with text."""

    async def fn(messages: Any, agent_info: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="show_checklist",
                        json_args=json.dumps(tool_args),
                        tool_call_id="cl1",
                    )
                }
            )
        else:
            yield "принято"

    return fn


ITEMS = [
    {"id": "ru", "text": "Русский язык"},
    {"id": "math", "text": "Математика", "checked": True},
    {"text": "Физика"},  # id defaults to "3"
]


# ---------- tool ----------


async def test_show_checklist_registered() -> None:
    from concierge.agent import tools

    assert "show_checklist" in tools.DYNAMIC_TOOLS
    assert "show_checklist" in tools.SUBAGENT_EXCLUDED
    assert "show_checklist" not in tools.NAMED_MODEL_TOOLS
    allow_all = {"tools": {"allow": ["*"], "deny": []}}
    assert "show_checklist" in tools.allowed_tool_names(allow_all)


async def test_show_checklist_validation(engine) -> None:
    from concierge.agent.tools import ChecklistItem, show_checklist

    deps = ToolDeps(user_id=1, conversation_id=0)
    ctx = SimpleNamespace(deps=deps)
    item = ChecklistItem(text="x")

    res = await show_checklist(ctx, [item])  # type: ignore[arg-type]
    assert res["ok"] is True
    res = await show_checklist(ctx, [item])  # type: ignore[arg-type]
    assert "error" in res  # one checklist per run

    part = deps.checklists[0]
    assert part["type"] == "checklist" and len(part["checklist_id"]) == 32
    assert part["columns"] == [] and part["submissions"] == 0
    assert part["submitted_by"] is None and part["submitted_at"] is None
    assert part["submit_label"] == "Отправить"
    assert part["skill"] is None
    assert part["items"] == [
        {"id": "1", "text": "x", "cells": [], "checked": False}
    ]

    ctx2 = SimpleNamespace(deps=ToolDeps(user_id=1, conversation_id=0))
    for bad in (
        [],
        [item] * 51,
        [ChecklistItem(text=" ")],
        [ChecklistItem(id="x" * 65, text="a")],
        [ChecklistItem(id="d", text="a"), ChecklistItem(id="d", text="b")],
        [ChecklistItem(text="a", cells=["1"])],  # cells without columns
    ):
        res = await show_checklist(ctx2, bad)  # type: ignore[arg-type]
        assert "error" in res, bad
    res = await show_checklist(
        ctx2, [item], columns=[f"c{i}" for i in range(7)]  # type: ignore[arg-type]
    )
    assert "error" in res
    res = await show_checklist(
        ctx2,
        [ChecklistItem(text="a", cells=["only"])],
        columns=["c1", "c2"],  # type: ignore[arg-type]
    )
    assert "error" in res  # len(cells) != len(columns)

    # Table mode: ids default "1".."n", cells kept.
    res = await show_checklist(
        ctx2,
        [
            ChecklistItem(text="a", cells=["1", "2"], checked=True),
            ChecklistItem(text="b", cells=["3", "4"]),
        ],
        title="Домашка",
        columns=["предмет", "задание"],  # type: ignore[arg-type]
    )
    assert res["ok"] is True
    part = ctx2.deps.checklists[0]
    assert part["title"] == "Домашка"
    assert part["columns"] == ["предмет", "задание"]
    assert part["items"][0] == {
        "id": "1",
        "text": "a",
        "cells": ["1", "2"],
        "checked": True,
    }
    assert part["items"][1]["id"] == "2"


async def test_show_checklist_records_skill(engine) -> None:
    from concierge.agent.tools import ChecklistItem, show_checklist

    deps = ToolDeps(
        user_id=1, conversation_id=0, skill_name="school", skill_id=7
    )
    ctx = SimpleNamespace(deps=deps)
    res = await show_checklist(ctx, [ChecklistItem(text="x")])  # type: ignore[arg-type]
    assert res["ok"] is True
    assert deps.checklists[0]["skill"] == "school"
    assert deps.checklists[0]["skill_id"] == 7


async def test_show_checklist_submit_label_normalized(engine) -> None:
    from concierge.agent.tools import ChecklistItem, show_checklist

    deps = ToolDeps(user_id=1, conversation_id=0)
    ctx = SimpleNamespace(deps=deps)
    res = await show_checklist(
        ctx, [ChecklistItem(text="x")], submit_label="   "  # type: ignore[arg-type]
    )
    assert res["ok"] is True
    assert deps.checklists[0]["submit_label"] == "Отправить"


# ---------- flow ----------


async def test_checklist_flow(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    calls = {"n": 0}
    infos, requests = await _bind_fn(
        monkeypatch, _cl_fn(calls, items=ITEMS, title="Домашка")
    )
    user = await make_user("cl", "adult")
    token = await login(client, "cl")
    cid = await _make_conv(client, token)
    initial = await _post_and_collect(client, token, cid, "составь чек-лист")
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    reply = msgs[-1]
    assert reply.author_kind == "assistant"
    part = reply.parts[-1]  # checklist is the last part of the reply
    assert part["type"] == "checklist"
    assert part["title"] == "Домашка"
    assert part["columns"] == []  # list mode
    assert part["submissions"] == 0 and part["submitted_by"] is None
    assert [i["id"] for i in part["items"]] == ["ru", "math", "3"]

    resp = await client.post(
        f"/conversations/{cid}/messages/{reply.id}/checklist",
        json={
            "checklist_id": part["checklist_id"],
            "checked": ["ru", "3"],
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["hidden"] is True
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    orig = next(m for m in msgs if m.id == reply.id)
    cl = next(p for p in orig.parts if p["type"] == "checklist")
    assert cl["submissions"] == 1
    assert cl["submitted_by"] == user.id and cl["submitted_at"]
    assert {i["id"]: i["checked"] for i in cl["items"]} == {
        "ru": True,
        "math": False,
        "3": True,
    }

    answer = next(
        m for m in msgs if any(p.get("checklist_answer") for p in m.parts)
    )
    assert answer.author_id == user.id
    assert answer.hidden is True
    ans = answer.parts[0]["checklist_answer"]
    assert ans["checklist_id"] == part["checklist_id"]
    assert ans["message_id"] == reply.id
    assert ans["skill"] is None
    assert ans["items"] == [
        {"id": "ru", "checked": True},
        {"id": "math", "checked": False},
        {"id": "3", "checked": True},
    ]
    assert ans["changed"] == [
        {"id": "ru", "checked": True},
        {"id": "math", "checked": False},
        {"id": "3", "checked": True},
    ]
    text = answer.parts[0]["text"]
    assert "Отмечено в «Домашка»" in text and "☑" in text and "☐" in text
    assert (
        answer.client_msg_id
        == f"cl:{reply.id}:1:{part['checklist_id'][:8]}"
    )

    # The submit scheduled a follow-up run that sees the structured state.
    runs = await _runs(cid)
    assert len(runs) == 2
    answer_run = next(r for r in runs if r.trigger_message_id == answer.id)
    followup = next(m for m in msgs if m.run_id == answer_run.id)
    assert followup.author_kind == "assistant"
    assert followup.hidden is True

    # The hidden exchange is folded into the checklist part itself: the
    # submit left a pending followups entry, the finished run filled it
    # with a compact copy of the reply (the card's "рассуждение").
    assert ans["n"] == 1
    assert len(cl["followups"]) == 1
    entry = cl["followups"][0]
    assert entry["n"] == 1 and entry["at"]
    assert "Отмечено в «Домашка»" in entry["answer"]
    assert entry["parts"] == [{"type": "text", "text": "принято"}]

    visible = await client.get(
        f"/conversations/{cid}/messages", headers=auth(token)
    )
    assert visible.status_code == 200
    assert [m["id"] for m in visible.json()] == [initial["id"], reply.id]
    assert all(m["hidden"] is False for m in visible.json())
    # The submission text now lives in the visible checklist part (inside
    # followups), so search finds the checklist message — but never the
    # hidden answer/reply rows themselves.
    found = await client.get(
        "/search", params={"q": "Отмечено"}, headers=auth(token)
    )
    assert found.status_code == 200
    assert [m["id"] for m in found.json()] == [reply.id]

    texts = _prompt_texts(requests[-1])
    joined = "\n".join(texts)
    assert "[чек-лист:" in joined
    assert '"ru": true' in joined and '"math": false' in joined
    assert '"changed": ["ru", "math", "3"]' in joined
    assert f"[Пользователь: {user.display_name}]" in infos[-1].instructions


async def test_checklist_submit_hides_realtime_traffic(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    calls = {"n": 0}
    await _bind_fn(monkeypatch, _cl_fn(calls, items=ITEMS))
    await make_user("clrt", "adult")
    token = await login(client, "clrt")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "чек-лист")
    await get_runtime().wait_idle()
    reply = (await _messages(cid))[-1]
    part = reply.parts[-1]

    from concierge.channels import get_hub

    events: list[str] = []

    class Sink:
        user_id = 0
        presence = False

        async def emit(self, event: Any) -> None:
            events.append(event.type)

    conn = get_hub().connect(Sink())
    await get_hub().subscribe(conn, cid)
    try:
        resp = await client.post(
            f"/conversations/{cid}/messages/{reply.id}/checklist",
            json={"checklist_id": part["checklist_id"], "checked": ["ru"]},
            headers=auth(token),
        )
        assert resp.status_code == 201
        await get_runtime().wait_idle()
    finally:
        await get_hub().disconnect(conn)

    assert "message.updated" in events
    assert "message.created" not in events
    assert "message.delta" not in events
    assert "message.tool" not in events
    assert "run.status" not in events


async def test_checklist_resubmit(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    calls = {"n": 0}
    await _bind_fn(monkeypatch, _cl_fn(calls, items=ITEMS, title="Домашка"))
    await make_user("cl2", "adult")
    token = await login(client, "cl2")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "чек-лист")
    await get_runtime().wait_idle()
    reply = (await _messages(cid))[-1]
    part = reply.parts[-1]
    url = f"/conversations/{cid}/messages/{reply.id}/checklist"

    resp = await client.post(
        url,
        json={"checklist_id": part["checklist_id"], "checked": ["ru"]},
        headers=auth(token),
    )
    assert resp.status_code == 201
    resp = await client.post(
        url,
        json={
            "checklist_id": part["checklist_id"],
            "checked": ["ru", "math", "3"],
        },
        headers=auth(token),
    )
    assert resp.status_code == 201
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    orig = next(m for m in msgs if m.id == reply.id)
    cl = next(p for p in orig.parts if p["type"] == "checklist")
    assert cl["submissions"] == 2
    assert all(i["checked"] for i in cl["items"])
    # Each submission got its own followups entry, filled in order.
    assert [f["n"] for f in cl["followups"]] == [1, 2]
    assert all(f["parts"] for f in cl["followups"])
    answers = [
        m for m in msgs if any(p.get("checklist_answer") for p in m.parts)
    ]
    assert len(answers) == 2
    # Second submit diffs against the last submitted state.
    snap = answers[-1].parts[0]["checklist_answer"]
    assert snap["changed"] == [
        {"id": "math", "checked": True},
        {"id": "3", "checked": True},
    ]
    assert answers[-1].client_msg_id == (
        f"cl:{reply.id}:2:{part['checklist_id'][:8]}"
    )
    assert len(await _runs(cid)) == 3


async def test_checklist_endpoint_errors(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    calls = {"n": 0}
    await _bind_fn(monkeypatch, _cl_fn(calls, items=ITEMS))
    await make_user("cl3", "adult")
    token = await login(client, "cl3")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "чек-лист")
    await get_runtime().wait_idle()
    reply = (await _messages(cid))[-1]
    part = reply.parts[-1]
    url = f"/conversations/{cid}/messages/{reply.id}/checklist"

    # Unknown item id -> 422.
    resp = await client.post(
        url,
        json={"checklist_id": part["checklist_id"], "checked": ["nope"]},
        headers=auth(token),
    )
    assert resp.status_code == 422
    # Unknown checklist id -> 404.
    resp = await client.post(
        url,
        json={"checklist_id": "0" * 32, "checked": []},
        headers=auth(token),
    )
    assert resp.status_code == 404
    # Unknown message id -> 404.
    resp = await client.post(
        f"/conversations/{cid}/messages/999999/checklist",
        json={"checklist_id": part["checklist_id"], "checked": []},
        headers=auth(token),
    )
    assert resp.status_code == 404
    # Non-member -> 404.
    await make_user("cl4", "adult")
    otoken = await login(client, "cl4")
    resp = await client.post(
        url,
        json={"checklist_id": part["checklist_id"], "checked": []},
        headers=auth(otoken),
    )
    assert resp.status_code == 404
    # Archived conversation -> 409.
    resp = await client.patch(
        f"/conversations/{cid}",
        json={"status": "archived"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    resp = await client.post(
        url,
        json={"checklist_id": part["checklist_id"], "checked": ["ru"]},
        headers=auth(token),
    )
    assert resp.status_code == 409


async def test_checklist_reactivates_ephemeral_and_wakes_mention(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """A checklist shown under an ephemeral skill records it; submitting
    re-injects the skill for the follow-up run and wakes a mention group."""
    calls = {"n": 0}
    infos, _ = await _bind_fn(
        monkeypatch, _cl_fn(calls, items=ITEMS, title="Домашка")
    )
    await make_user("clown", "adult")
    member = await make_user("clmem", "adult")
    skill = await _add_skill(member.id, SKILL_MD_EPHEMERAL)
    token = await login(client, "clown")
    resp = await client.post(
        "/conversations",
        json={"kind": "group", "title": "g", "member_ids": [member.id]},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    mtoken = await login(client, "clmem")

    # Group reply_mode is 'mention': the /command run is allowed through.
    await _post_and_collect(client, mtoken, cid, "/школа")
    await get_runtime().wait_idle()
    reply = (await _messages(cid))[-1]
    part = reply.parts[-1]
    assert part["type"] == "checklist" and part["skill"] == "school"
    assert part["skill_id"] == skill.id

    resp = await client.post(
        f"/conversations/{cid}/messages/{reply.id}/checklist",
        json={"checklist_id": part["checklist_id"], "checked": ["ru"]},
        headers=auth(mtoken),
    )
    assert resp.status_code == 201, resp.text
    await get_runtime().wait_idle()

    # The submit alone (no mention) produced a second run that re-injected
    # the ephemeral skill's instructions.
    assert len(await _runs(cid)) == 2
    assert "school_grades" in infos[-1].instructions
    assert "Скилл: school — на этот ответ" in infos[-1].instructions
    msgs = await _messages(cid)
    answer = next(
        m for m in msgs if any(p.get("checklist_answer") for p in m.parts)
    )
    snap = answer.parts[0]["checklist_answer"]
    assert snap["skill"] == "school" and snap["skill_id"] == skill.id
    # Ephemeral: still nothing pinned, no skill_event rows.
    assert not [
        p for m in msgs for p in m.parts if p.get("type") == "skill_event"
    ]


async def test_checklist_unchanged_resubmit_deduped(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """Re-sending the stored state returns the first answer — no dup
    message, no extra run."""
    calls = {"n": 0}
    await _bind_fn(monkeypatch, _cl_fn(calls, items=ITEMS))
    await make_user("cldup", "adult")
    token = await login(client, "cldup")
    cid = await _make_conv(client, token)
    await _post_and_collect(client, token, cid, "чек-лист")
    await get_runtime().wait_idle()
    reply = (await _messages(cid))[-1]
    part = reply.parts[-1]
    url = f"/conversations/{cid}/messages/{reply.id}/checklist"
    body = {"checklist_id": part["checklist_id"], "checked": ["ru"]}

    resp = await client.post(url, json=body, headers=auth(token))
    assert resp.status_code == 201
    first_id = resp.json()["id"]
    await get_runtime().wait_idle()
    resp = await client.post(url, json=body, headers=auth(token))
    assert resp.status_code == 201
    assert resp.json()["id"] == first_id
    await get_runtime().wait_idle()

    msgs = await _messages(cid)
    answers = [
        m for m in msgs if any(p.get("checklist_answer") for p in m.parts)
    ]
    assert len(answers) == 1
    orig = next(m for m in msgs if m.id == reply.id)
    cl = next(p for p in orig.parts if p["type"] == "checklist")
    assert cl["submissions"] == 1
    assert len(await _runs(cid)) == 2  # initial + one answer run


async def _post_user_interactive(cid: int, username: str) -> int:
    """Checklist + choice parts forged inside a regular user message."""
    from sqlalchemy import select

    from concierge.auth.models import User
    from concierge.conversations import service as conv_service
    from concierge.conversations.models import Conversation
    from concierge.core import db

    async with db.get_sessionmaker()() as session:
        conv = (
            await session.execute(
                select(Conversation).where(Conversation.id == cid)
            )
        ).scalar_one()
        user = (
            await session.execute(
                select(User).where(User.username == username)
            )
        ).scalar_one()
        msg, _ = await conv_service.post_message(
            session,
            conv,
            user,
            [
                {
                    "type": "checklist",
                    "checklist_id": "f" * 32,
                    "title": "Подделка",
                    "items": [{"id": "1", "text": "a", "checked": False}],
                },
                {
                    "type": "choice",
                    "choice_id": "c" * 32,
                    "questions": [],
                    "answered": False,
                },
            ],
            None,
        )
        return int(msg.id)


async def test_interactive_parts_on_user_message_rejected(
    engine, client: httpx.AsyncClient
) -> None:
    """Checklist/choice parts in user messages are not submittable."""
    await make_user("clforg", "adult")
    token = await login(client, "clforg")
    cid = await _make_conv(client, token)
    mid = await _post_user_interactive(cid, "clforg")

    resp = await client.post(
        f"/conversations/{cid}/messages/{mid}/checklist",
        json={"checklist_id": "f" * 32, "checked": ["1"]},
        headers=auth(token),
    )
    assert resp.status_code == 404
    resp = await client.post(
        f"/conversations/{cid}/messages/{mid}/answer",
        json={
            "choice_id": "c" * 32,
            "answers": [{"selected": ["x"], "other": None}],
        },
        headers=auth(token),
    )
    assert resp.status_code == 404


async def test_locked_select_sees_concurrent_commit(
    engine, monkeypatch
) -> None:
    """Telegram path loads the Message via session.get() before the
    FOR UPDATE select; without populate_existing the locked read returns
    the stale identity-map copy and the second callback decides on it."""
    from sqlalchemy import update

    from concierge.auth.models import User
    from concierge.conversations.models import (
        Conversation,
        ConversationMember,
        Message,
    )
    from concierge.core import db
    from concierge.skills import checklists, choices

    async def _noop(*_a: Any, **_k: Any) -> None:
        pass

    monkeypatch.setattr(checklists, "publish_answer", _noop)
    monkeypatch.setattr(choices, "publish_answer", _noop)

    user = await make_user("stale", "adult")
    async with db.get_sessionmaker()() as session:
        u = await session.get(User, user.id)
        assert u is not None
        conv = Conversation(kind="group", title="g", owner_id=u.id)
        conv.members.append(ConversationMember(user_id=u.id))
        session.add(conv)
        await session.flush()
        cl_id, ch_id = "d" * 32, "e" * 32
        cl_part = {
            "type": "checklist",
            "checklist_id": cl_id,
            "title": "t",
            "items": [{"id": "a", "text": "A", "checked": False}],
            "submissions": 0,
        }
        ch_part = {
            "type": "choice",
            "choice_id": ch_id,
            "questions": [],
            "answered": False,
        }
        cl_msg = Message(
            conversation_id=conv.id,
            seq=1,
            author_kind="assistant",
            parts=[cl_part],
        )
        ch_msg = Message(
            conversation_id=conv.id,
            seq=2,
            author_kind="assistant",
            parts=[ch_part],
        )
        session.add_all([cl_msg, ch_msg])
        await session.commit()

        # TG handler: session.get() + attribute access materializes the
        # rows; a racing request then commits new state, leaving the
        # identity-map copies stale.
        cl_stale = await session.get(Message, cl_msg.id)
        ch_stale = await session.get(Message, ch_msg.id)
        assert cl_stale is not None and ch_stale is not None
        assert cl_stale.parts[0]["submissions"] == 0
        assert ch_stale.parts[0]["answered"] is False
        await session.execute(
            update(Message.__table__)
            .where(Message.__table__.c.id == cl_msg.id)
            .values(
                parts=[
                    {
                        **cl_part,
                        "items": [
                            {"id": "a", "text": "A", "checked": True}
                        ],
                        "submissions": 1,
                    }
                ]
            )
        )
        await session.execute(
            update(Message.__table__)
            .where(Message.__table__.c.id == ch_msg.id)
            .values(parts=[{**ch_part, "answered": True}])
        )

        reply = await checklists.submit_checklist(
            session, conv, u, cl_msg.id, cl_id, [], "telegram"
        )
        cl = next(p for p in cl_stale.parts if p["type"] == "checklist")
        assert cl["submissions"] == 2  # would be 1 decided on stale parts
        snap = reply.parts[0]["checklist_answer"]
        assert snap["changed"] == [{"id": "a", "checked": False}]

        with pytest.raises(choices.ChoiceError, match="answered"):
            await choices.answer_choice(
                session,
                conv,
                u,
                ch_msg.id,
                ch_id,
                [{"selected": ["x"], "other": None}],
                "telegram",
            )


async def test_checklist_history_render() -> None:
    from concierge.agent import history as agent_history

    part = {
        "type": "checklist",
        "checklist_id": "x",
        "title": "Домашка",
        "items": [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}],
    }
    rendered = agent_history.render_for_summary([part])
    assert "[чек-лист] Домашка (2 пунктов)" in rendered
    # A stored checklist part survives history replay (skipped, not crash).
    msgs = agent_history.deserialize_parts(
        [{"type": "text", "text": "вот список"}, part]
    )
    assert msgs  # text part survived; checklist part skipped


async def test_compact_followup_parts() -> None:
    from concierge.skills.checklists import compact_followup_parts

    parts = [
        {"type": "thinking", "content": "думаю", "signature": "sig"},
        {
            "type": "tool_call",
            "name": "web_search",
            "tool_call_id": "t1",
            "args": {"q": "x" * 5000},
        },
        {"type": "tool_return", "tool_call_id": "t1", "content": "y" * 5000},
        {"type": "tool_call", "name": "bad_tool", "tool_call_id": "t2"},
        {"type": "tool_return", "tool_call_id": "t2", "retry": True},
        {"type": "text", "text": "готово"},
        {"type": "text", "text": ""},
        {"type": "image", "attachment_id": 5},
    ]
    assert compact_followup_parts(parts) == [
        {"type": "thinking", "content": "думаю"},
        {"type": "tool", "name": "web_search", "status": "done"},
        {"type": "tool", "name": "bad_tool", "status": "error"},
        {"type": "text", "text": "готово"},
    ]
    assert compact_followup_parts([]) == []
