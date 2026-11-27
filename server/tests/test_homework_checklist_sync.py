"""Homework-done imports reconciling already-rendered checklist cards."""

import copy
import json
import logging
from typing import Any

from sqlalchemy import select

from concierge.channels import get_hub
from concierge.conversations.models import (
    Conversation,
    ConversationMember,
    Message,
)
from concierge.core import db
from concierge.skills.checklists import (
    extract_school_homework_done_updates,
    sync_school_homework_done_checklists,
)
from tests.conftest import make_user


def _import_call(
    call_id: str,
    rows: list[dict[str, Any]],
    *,
    table: str = "school_homework_done",
    mode: str | None = "append",
) -> dict[str, Any]:
    args: dict[str, Any] = {"table": table, "rows": rows}
    if mode is not None:
        args["mode"] = mode
    return {
        "type": "tool_call",
        "name": "analytics_import_rows",
        "tool_call_id": call_id,
        "args": args,
    }


def _import_return(
    call_id: str,
    inserted: int,
    *,
    table: str = "sandbox.school_homework_done",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "tool_return",
        "name": "analytics_import_rows",
        "tool_call_id": call_id,
        "content": {"table": table, "inserted": inserted, "columns": []},
        **extra,
    }


def test_extract_school_homework_done_updates_success() -> None:
    rows = [
        {"hw_id": "hwgeo", "done": True},
        {"hw_id": "hwmath", "done": False},
        {"hw_id": "bad-done", "done": 1},
        {"hw_id": "", "done": True},
        {"hw_id": None, "done": True},
        {"done": True},
        {"hw_id": "missing-done"},
        "not-a-row",
    ]
    parts = [
        {"type": "text", "text": "отмечаю"},
        _import_call("c1", rows, mode=None),  # omitted mode is append by tool default
        _import_return("c1", len(rows)),
        {"type": "text", "text": "готово"},
    ]
    assert extract_school_homework_done_updates(parts) == {
        "hwgeo": True,
        "hwmath": False,
    }


def test_extract_school_homework_done_updates_last_write_wins() -> None:
    string_args_call = _import_call(
        "c1",
        [
            {"hw_id": "hwgeo", "done": True},
            {"hw_id": "hwgeo", "done": False},
        ],
    )
    # Some providers/streamed calls persist args as a JSON string.
    string_args_call["args"] = json.dumps(string_args_call["args"])
    parts = [
        string_args_call,
        _import_return("c1", 2),
        _import_call("c2", [{"hw_id": "hwgeo", "done": True}]),
        _import_return("c2", 1),
    ]
    assert extract_school_homework_done_updates(parts) == {"hwgeo": True}


def test_extract_school_homework_done_updates_rejects_nonconfirming_parts() -> None:
    row = {"hw_id": "hwgeo", "done": True}
    cases = [
        [_import_call("c1", [row])],  # no return
        [
            {**_import_call("c1", [row]), "name": "other_import_rows"},
            _import_return("c1", 1),
        ],
        [
            _import_call("c1", [row], table="other_table"),
            _import_return("c1", 1),
        ],
        [
            _import_call("c1", [row], table="sandbox.school_homework_done"),
            _import_return("c1", 1),
        ],
        [
            _import_call("c1", [row], mode="replace"),
            _import_return("c1", 1),
        ],
        [
            _import_call("c1", []),
            _import_return("c1", 0),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 0),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 2),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 1, table="sandbox.other_table"),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 1, retry=True),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 1, outcome="failed"),
        ],
        [
            _import_call("c1", [row]),
            _import_return("c1", 1, outcome="denied"),
        ],
        [
            _import_call("c1", [row]),
            {**_import_return("c1", 1), "content": "inserted"},
        ],
        [
            _import_call("c1", [row]),
            {**_import_return("c1", 1), "content": {"table": "sandbox.school_homework_done"}},
        ],
        [
            {**_import_call("c1", [row]), "args": "not-a-dict"},
            _import_return("c1", 1),
        ],
        [
            _import_call("c1", [row]),
            {**_import_return("c1", 1), "tool_call_id": "other"},
        ],
        [
            _import_return("c1", 1),
            _import_call("c1", [row]),  # return before its call is not a pair
        ],
    ]
    for parts in cases:
        assert extract_school_homework_done_updates(parts) == {}, parts


def _homework_part(checked: bool = False) -> dict[str, Any]:
    return {
        "type": "checklist",
        "checklist_id": "a" * 32,
        "title": "Домашка",
        "items": [
            {
                "id": "hwgeo",
                "text": "География",
                "cells": ["карта"],
                "checked": checked,
            },
            {"id": "hwmath", "text": "Математика", "checked": False},
        ],
        "submissions": 2,
        "submitted_by": 17,
        "submitted_at": "2026-01-01T10:00:00",
        "followups": [
            {
                "n": 1,
                "answer": "Отмечено",
                "at": "2026-01-01T10:00:00",
                "parts": [{"type": "text", "text": "принято"}],
            }
        ],
        "skill": "school",
        "skill_id": 7,
        "custom": "keep",
    }


async def test_sync_school_homework_done_checklists_rewrites_visible_cards(
    engine,
) -> None:
    user = await make_user("hwsync", "adult")
    async with db.get_sessionmaker()() as session:
        conv = Conversation(kind="personal", title="t", owner_id=user.id)
        conv.members.append(ConversationMember(user_id=user.id))
        other = Conversation(kind="personal", title="o", owner_id=user.id)
        other.members.append(ConversationMember(user_id=user.id))
        session.add_all([conv, other])
        await session.flush()

        part1 = _homework_part()
        part2 = _homework_part()
        part2["checklist_id"] = "b" * 32
        hidden_part = _homework_part()
        user_part = _homework_part()
        other_part = _homework_part()
        msgs = [
            Message(
                conversation_id=conv.id,
                seq=1,
                author_kind="assistant",
                parts=[
                    {"type": "text", "text": "вот домашка"},
                    part1,
                    {"type": "text", "text": "tail"},
                ],
            ),
            Message(
                conversation_id=conv.id,
                seq=2,
                author_kind="assistant",
                parts=[part2],
            ),
            Message(
                conversation_id=conv.id,
                seq=3,
                author_kind="assistant",
                hidden=True,
                parts=[hidden_part],
            ),
            Message(
                conversation_id=conv.id,
                seq=4,
                author_kind="user",
                author_id=user.id,
                parts=[user_part],
            ),
            Message(
                conversation_id=conv.id,
                seq=5,
                author_kind="assistant",
                parts=[{"type": "text", "text": "no checklist"}],
            ),
            Message(
                conversation_id=other.id,
                seq=1,
                author_kind="assistant",
                parts=[other_part],
            ),
        ]
        session.add_all(msgs)
        await session.commit()
        conv_id = conv.id
        first_id, second_id, hidden_id, user_msg_id, plain_id, other_msg_id = [
            m.id for m in msgs
        ]
        before_part1 = copy.deepcopy(part1)

    events: list[Any] = []

    class Sink:
        user_id = 0
        presence = False

        async def emit(self, event: Any) -> None:
            events.append(event)

    conn = get_hub().connect(Sink())
    await get_hub().subscribe(conn, conv_id)
    try:
        changed = await sync_school_homework_done_checklists(
            conv_id, {"hwgeo": True}
        )
    finally:
        await get_hub().disconnect(conn)

    assert changed == 2
    updates = [e for e in events if e.type == "message.updated"]
    assert [e.data["message"]["id"] for e in updates] == [first_id, second_id]
    assert all(e.conversation_id == conv_id for e in updates)

    async with db.get_sessionmaker()() as session:
        rows = {
            msg.id: msg
            for msg in (
                await session.execute(
                    select(Message).where(
                        Message.id.in_(
                            [
                                first_id,
                                second_id,
                                hidden_id,
                                user_msg_id,
                                plain_id,
                                other_msg_id,
                            ]
                        )
                    )
                )
            ).scalars()
        }

    def part(msg_id: int) -> dict[str, Any]:
        return next(
            p for p in rows[msg_id].parts if p.get("type") == "checklist"
        )

    for msg_id in (first_id, second_id):
        cl = part(msg_id)
        states = {item["id"]: item["checked"] for item in cl["items"]}
        assert states == {"hwgeo": True, "hwmath": False}
        for key in (
            "submissions",
            "submitted_by",
            "submitted_at",
            "followups",
            "skill",
            "skill_id",
            "custom",
            "title",
        ):
            assert cl[key] == before_part1[key]
        assert cl["items"][0]["cells"] == ["карта"]

    assert rows[first_id].parts[0] == {"type": "text", "text": "вот домашка"}
    assert rows[first_id].parts[2] == {"type": "text", "text": "tail"}
    assert part(hidden_id)["items"][0]["checked"] is False
    assert part(user_msg_id)["items"][0]["checked"] is False
    assert part(other_msg_id)["items"][0]["checked"] is False
    assert rows[plain_id].parts == [{"type": "text", "text": "no checklist"}]

    # The reverse write clears the same checkboxes.
    changed = await sync_school_homework_done_checklists(
        conv_id, {"hwgeo": False}
    )
    assert changed == 2
    async with db.get_sessionmaker()() as session:
        for msg_id in (first_id, second_id):
            msg = await session.get(Message, msg_id)
            assert msg is not None
            cl = next(p for p in msg.parts if p.get("type") == "checklist")
            assert cl["items"][0]["checked"] is False

    # Already-correct state is a no-op (no rewrite, no event).
    events.clear()
    conn = get_hub().connect(Sink())
    await get_hub().subscribe(conn, conv_id)
    try:
        changed = await sync_school_homework_done_checklists(
            conv_id, {"hwgeo": False}
        )
    finally:
        await get_hub().disconnect(conn)
    assert changed == 0
    assert not [e for e in events if e.type == "message.updated"]


async def test_runtime_homework_sync_failure_is_logged(monkeypatch, caplog) -> None:
    from concierge.agent.runtime import get_runtime
    from concierge.skills import checklists

    async def fail_sync(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("db is down")

    monkeypatch.setattr(
        checklists, "sync_school_homework_done_checklists", fail_sync
    )
    parts = [
        _import_call("c1", [{"hw_id": "hwgeo", "done": True}]),
        _import_return("c1", 1),
    ]
    with caplog.at_level(logging.ERROR, logger="concierge.agent.runtime"):
        await get_runtime()._sync_homework_checklists(1, parts)
    assert "homework checklist sync failed conv=1" in caplog.text
