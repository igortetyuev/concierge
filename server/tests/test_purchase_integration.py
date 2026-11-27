# ruff: noqa: RUF001
"""Cross-stage purchase integration tests.

These tests intentionally exercise the boundaries that unit tests cannot cover
by themselves: seeded skill activation in the real agent runtime, interactive
checklist answers, purchase tools, durable service state, and rendering.
"""

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, DeltaToolCalls
from sqlalchemy import select

from concierge.agent.runtime import get_runtime
from concierge.agent.tools import ToolDeps
from concierge.core import db
from concierge.media import tts
from concierge.purchases import service
from concierge.purchases.default_skill import DEFAULT_SKILL_CONTENT
from concierge.purchases.models import PurchaseItem
from concierge.purchases.tools import (
    PurchaseOperation,
    purchase_query,
    purchase_share,
    purchase_store,
    purchase_update,
)
from concierge.skills import service as skill_service
from tests.conftest import auth, login, make_user
from tests.test_agent import _make_conv, _post_and_collect
from tests.test_skill_runtime import _messages, _runs, _use_function_model


def _ctx(user_id: int, trigger_text: str = "") -> Any:
    return SimpleNamespace(
        deps=ToolDeps(user_id=user_id, conversation_id=0, trigger_text=trigger_text)
    )


def _op(action: str, **kwargs: Any) -> PurchaseOperation:
    return PurchaseOperation.model_validate({"action": action, **kwargs})


def _purchase_query_item(messages: Any, item_name: str) -> dict[str, Any]:
    """Extract an item from the latest purchase_query tool return.

    This mirrors what a real model can do: tool arguments in the next model step
    are derived from data visible in the preceding tool result, not from test
    state captured outside the conversation.
    """
    for message in reversed(messages):
        for part in reversed(message.parts):
            if not isinstance(part, ToolReturnPart) or part.tool_name != "purchase_query":
                continue
            content = part.content
            if isinstance(content, str):
                content = json.loads(content)
            assert isinstance(content, dict)
            for plist in content.get("lists", []):
                for item in plist.get("items", []):
                    if item.get("name") == item_name:
                        return item
            raise AssertionError(f"{item_name!r} not found in purchase_query result")
    raise AssertionError("purchase_query tool result is not visible to the model")


def _changed_checklist_item(messages: Any) -> tuple[int, bool]:
    """Extract the changed exact item id/state from the checklist prompt."""
    prefix = "[чек-лист: "
    for message in reversed(messages):
        for part in reversed(message.parts):
            if not isinstance(part, UserPromptPart) or not isinstance(part.content, str):
                continue
            for line in part.content.splitlines():
                start = line.find(prefix)
                if start < 0 or not line.endswith("]"):
                    continue
                payload = json.loads(line[start + len(prefix) : -1])
                changed = payload["changed"]
                assert len(changed) == 1
                item_id = str(changed[0])
                return int(item_id), bool(payload["items"][item_id])
    raise AssertionError("checklist answer state is not visible to the model")


async def test_seeded_purchases_skill_runtime_query_chains_checklist_and_submits(
    engine, client: httpx.AsyncClient, monkeypatch
) -> None:
    """A deterministic end-to-end purchase conversation through the runtime.

    The fake model performs realistic sequential tool decisions: it first calls
    purchase_query, then derives the checklist item id from the visible query
    tool result in the next model step. Checklist mutations likewise derive the
    exact item id and desired state from the structured checklist answer prompt.
    All downstream behavior is real: ephemeral skill activation, purchase
    service mutations, rendering, persistence, hidden follow-ups, and dedupe.
    """
    calls = {"n": 0}

    def update_call(operations: list[dict[str, Any]], call_id: str) -> DeltaToolCalls:
        return DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="purchase_update",
                    json_args=json.dumps({"operations": operations}),
                    tool_call_id=call_id,
                )
            }
        )

    async def model_script(messages: Any, agent_info: Any):
        del agent_info
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            yield update_call(
                [
                    {
                        "action": "add",
                        "name": "молоко",
                        "quantity": 2,
                        "unit": "литра",
                        "category": "молочные",
                        "general": True,
                    }
                ],
                "purchase-add",
            )
        elif n == 2:
            yield "Добавил молоко."
        elif n == 3:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="purchase_query",
                        json_args=json.dumps({"general": True, "status": "all"}),
                        tool_call_id="purchase-query",
                    )
                }
            )
        elif n == 4:
            item = _purchase_query_item(messages, "молоко")
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="show_checklist",
                        json_args=json.dumps(
                            {
                                "title": "Покупки",
                                "items": [
                                    {
                                        "id": str(item["id"]),
                                        "text": (
                                            f"{item['name']} — "
                                            f"{item['quantity']} {item['unit']}"
                                        ),
                                        "checked": item["status"] == "bought",
                                    }
                                ],
                            }
                        ),
                        tool_call_id="purchase-checklist",
                    )
                }
            )
        elif n == 5:
            yield "Вот текущий список."
        elif n in (6, 8):
            item_id, checked = _changed_checklist_item(messages)
            yield update_call(
                [
                    {
                        "action": "mark_bought" if checked else "mark_pending",
                        "item_id": item_id,
                    }
                ],
                f"purchase-transition-{n}",
            )
        elif n == 7:
            yield "Отметил молоко купленным."
        else:
            yield "Вернул молоко в список."

    infos = await _use_function_model(monkeypatch, model_script)
    user = await make_user("pi_runtime", "adult")
    async with db.get_sessionmaker()() as session:
        skill = await skill_service.create_skill(
            session,
            None,
            DEFAULT_SKILL_CONTENT,
            created_by=user.id,
            role_ids=[],
        )
        skill_id = skill.id

    token = await login(client, "pi_runtime")
    conversation_id = await _make_conv(client, token)

    await _post_and_collect(client, token, conversation_id, "надо купить молоко")
    await get_runtime().wait_idle()
    assert "[Скилл: purchases — на этот ответ]" in (infos[0].instructions or "")
    assert "purchase_update" in (infos[0].instructions or "")

    async with db.get_sessionmaker()() as session:
        item = (
            await session.execute(select(PurchaseItem).where(PurchaseItem.name == "молоко"))
        ).scalar_one()
        assert item.status == "pending"
        assert item.quantity == 2

    await _post_and_collect(client, token, conversation_id, "что осталось купить?")
    await get_runtime().wait_idle()
    assert "[Скилл: purchases — на этот ответ]" in (infos[2].instructions or "")

    messages = await _messages(conversation_id)
    checklist_reply = messages[-1]
    assert checklist_reply.author_kind == "assistant"
    checklist_part = next(
        p for p in checklist_reply.parts if p.get("type") == "checklist"
    )
    assert checklist_part["skill"] == "purchases"
    assert checklist_part["skill_id"] == skill_id
    assert len(checklist_part["items"]) == 1
    item_id = int(checklist_part["items"][0]["id"])
    assert checklist_part["items"][0]["checked"] is False

    query_return = next(
        p
        for p in checklist_reply.parts
        if p.get("type") == "tool_return" and p.get("name") == "purchase_query"
    )
    assert "# Покупки" in str(query_return["content"])
    assert "markdown" in str(query_return["content"])
    assert "spoken_text" in str(query_return["content"])
    final_text = next(
        p
        for p in checklist_reply.parts
        if p.get("type") == "text" and p.get("text") == "Вот текущий список."
    )
    assert "осталось купить" in final_text["spoken"]
    assert "молоко" in final_text["spoken"]
    assert tts.spoken_override(checklist_reply.parts) == final_text["spoken"]

    first_response = await client.post(
        f"/conversations/{conversation_id}/messages/{checklist_reply.id}/checklist",
        json={"checklist_id": checklist_part["checklist_id"], "checked": [str(item_id)]},
        headers=auth(token),
    )
    assert first_response.status_code == 201, first_response.text
    first_answer_id = first_response.json()["id"]
    await get_runtime().wait_idle()
    assert "[Скилл: purchases — на этот ответ]" in (infos[5].instructions or "")

    async with db.get_sessionmaker()() as session:
        item = await session.get(PurchaseItem, item_id)
        assert item is not None
        assert item.status == "bought"
        assert item.bought_by == user.id
    runs_after_first_submit = len(await _runs(conversation_id))
    assert runs_after_first_submit == 3

    duplicate_response = await client.post(
        f"/conversations/{conversation_id}/messages/{checklist_reply.id}/checklist",
        json={"checklist_id": checklist_part["checklist_id"], "checked": [str(item_id)]},
        headers=auth(token),
    )
    assert duplicate_response.status_code == 201, duplicate_response.text
    assert duplicate_response.json()["id"] == first_answer_id
    await get_runtime().wait_idle()
    assert len(await _runs(conversation_id)) == runs_after_first_submit

    uncheck_response = await client.post(
        f"/conversations/{conversation_id}/messages/{checklist_reply.id}/checklist",
        json={"checklist_id": checklist_part["checklist_id"], "checked": []},
        headers=auth(token),
    )
    assert uncheck_response.status_code == 201, uncheck_response.text
    assert uncheck_response.json()["id"] != first_answer_id
    await get_runtime().wait_idle()

    async with db.get_sessionmaker()() as session:
        item = await session.get(PurchaseItem, item_id)
        assert item is not None
        assert item.status == "pending"
    assert len(await _runs(conversation_id)) == 4
    assert calls["n"] == 9

    messages = await _messages(conversation_id)
    refreshed_reply = next(m for m in messages if m.id == checklist_reply.id)
    refreshed_part = next(
        p for p in refreshed_reply.parts if p.get("type") == "checklist"
    )
    assert refreshed_part["submissions"] == 2
    assert refreshed_part["items"][0]["checked"] is False
    assert len(refreshed_part["followups"]) == 2
    assert all(followup["parts"] for followup in refreshed_part["followups"])
    assert any(
        part.get("type") == "tool" and part.get("name") == "purchase_update"
        for part in refreshed_part["followups"][0]["parts"]
    )

    # The public REST feed carries the interactive card and spoken override,
    # while the hidden checklist answer/follow-up exchange stays folded into it.
    hidden_ids = {message.id for message in messages if message.hidden}
    assert hidden_ids
    feed_response = await client.get(
        f"/conversations/{conversation_id}/messages?limit=100",
        headers=auth(token),
    )
    assert feed_response.status_code == 200, feed_response.text
    feed = feed_response.json()
    assert not hidden_ids & {message["id"] for message in feed}
    api_reply = next(message for message in feed if message["id"] == checklist_reply.id)
    api_text = next(
        part
        for part in api_reply["parts"]
        if part.get("type") == "text" and part.get("text") == "Вот текущий список."
    )
    assert api_text["spoken"] == final_text["spoken"]
    api_query_return = next(
        part
        for part in api_reply["parts"]
        if part.get("type") == "tool_return" and part.get("name") == "purchase_query"
    )
    assert "# Покупки" in str(api_query_return["content"])


async def test_store_context_lifecycle_merge_correct_multiply_plan_and_rendering(
    engine,
) -> None:
    """One realistic shopping lifecycle through tools, service, and renderers."""
    user = await make_user("pi_lifecycle", "adult")

    def fresh_ctx() -> Any:
        return _ctx(user.id)

    ctx = fresh_ctx()
    entered = await purchase_store(ctx, action="set", store="пятёрочка")
    assert entered["ok"] is True
    assert entered["store_context"]["store_key"] == "pyaterochka"

    added = await purchase_update(
        fresh_ctx(),
        [
            _op("add", name="молоко", quantity=1, unit="литра"),
            _op("add", name="яблоки", quantity=500, unit="г"),
            _op("add", name="хлеб", quantity=1),
            _op("add", name="печенье", quantity=1),
        ],
    )
    assert added["ok"] is True, added
    assert {
        row["purchase_list"]["store_key"] for row in added["results"]
    } == {"pyaterochka"}

    merged = await purchase_update(
        fresh_ctx(), [_op("add", name="яблоки", quantity=0.5, unit="кг")]
    )
    merged_item = merged["results"][0]["item"]
    assert merged["ok"] is True
    assert merged_item["quantity"] == 1000
    assert merged_item["unit"] == "г"

    unspecified_duplicate = await purchase_update(
        fresh_ctx(), [_op("add", name="Яблоки")]
    )
    duplicate_item = unspecified_duplicate["results"][0]["item"]
    assert unspecified_duplicate["ok"] is True
    assert duplicate_item["id"] == merged_item["id"]
    assert duplicate_item["quantity"] == 1000

    corrected = await purchase_update(
        fresh_ctx(), [_op("set", name="яблоки", quantity=2, unit="кг")]
    )
    assert corrected["ok"] is True
    assert corrected["results"][0]["item"]["quantity"] == 2
    assert corrected["results"][0]["item"]["unit"] == "кг"

    multiplied = await purchase_update(
        fresh_ctx(), [_op("multiply", name="молоко", factor=1.5)]
    )
    assert multiplied["ok"] is True
    assert multiplied["results"][0]["item"]["quantity"] == 1.5
    assert multiplied["results"][0]["item"]["unit"] == "л"

    tomorrow = service.family_today() + timedelta(days=1)
    planned = await purchase_update(
        fresh_ctx(),
        [_op("add", name="зубная паста", quantity=1, planned_for=tomorrow)],
    )
    assert planned["ok"] is True

    removed = await purchase_update(
        fresh_ctx(), [_op("remove", name="печенье")]
    )
    assert removed["ok"] is True

    bought = await purchase_update(
        fresh_ctx(),
        [
            _op(
                "mark_bought",
                name="хлеб",
                price=55,
                price_kind="total",
            )
        ],
    )
    assert bought["ok"] is True
    assert bought["results"][0]["item"]["status"] == "bought"
    assert bought["results"][0]["item"]["price"] == 55

    query_ctx = fresh_ctx()
    current = await purchase_query(query_ctx, status="all")
    current_names = {
        item["name"] for plist in current["lists"] for item in plist["items"]
    }
    assert current_names == {"молоко", "яблоки", "хлеб"}
    assert "зубная паста" not in current_names
    assert "## Купить в Пятёрочка" in current["markdown"]
    assert "- [x] 1 шт хлеб" in current["markdown"]
    assert "55 RUB итого" in current["markdown"]
    assert "уже купили хлеб" in current["spoken_text"]
    assert query_ctx.deps.spoken_text == current["spoken_text"]

    future = await purchase_query(fresh_ctx(), status="all", include_future=True)
    future_names = {
        item["name"] for plist in future["lists"] for item in plist["items"]
    }
    assert "зубная паста" in future_names

    cleared = await purchase_store(fresh_ctx(), action="clear")
    assert cleared["ok"] is True and cleared["cleared"] is True
    general_add = await purchase_update(
        fresh_ctx(), [_op("add", name="соль", quantity=1)]
    )
    assert general_add["ok"] is True
    assert general_add["results"][0]["purchase_list"]["store_key"] == ""

    separated = await purchase_query(fresh_ctx(), status="all")
    separated_names = {
        item["name"] for plist in separated["lists"] for item in plist["items"]
    }
    assert separated_names == {"соль"}


async def test_shared_editor_assignee_and_permission_boundaries_across_tools(
    engine,
) -> None:
    """Multi-user visibility and mutation permissions survive tool/service layers."""
    owner = await make_user("pi_owner", "adult")
    editor = await make_user("pi_editor", "adult")
    observer = await make_user("pi_observer", "adult")
    assignee = await make_user("pi_assignee", "child")
    viewer = await make_user("pi_viewer", "guest")

    added = await purchase_update(
        _ctx(owner.id),
        [
            _op(
                "add",
                name="хлеб",
                quantity=1,
                store="spar",
                assignee=assignee.display_name,
            ),
            _op("add", name="молоко", quantity=1, store="spar"),
        ],
    )
    assert added["ok"] is True, added
    bread = added["results"][0]["item"]
    milk = added["results"][1]["item"]
    list_id = added["results"][0]["purchase_list"]["id"]

    observer_view = await purchase_query(_ctx(observer.id), store="spar", status="all")
    observer_lists = [
        plist for plist in observer_view["lists"] if plist["id"] == list_id
    ]
    assert len(observer_lists) == 1
    assert observer_lists[0]["access"] == "family"
    assert {item["name"] for item in observer_lists[0]["items"]} == {"хлеб", "молоко"}

    family_mutation = await purchase_update(
        _ctx(observer.id), [_op("set", item_id=milk["id"], quantity=3)]
    )
    family_row = family_mutation["results"][0]
    assert family_row["ok"] is False
    assert family_row["error"] == "denied"
    assert family_row["item"] is None
    assert family_row["purchase_list"] is None

    shared_editor = await purchase_share(
        _ctx(owner.id), [editor.display_name], store="spar"
    )
    shared_viewer = await purchase_share(
        _ctx(owner.id), [viewer.display_name], store="spar", mode="viewer"
    )
    assert shared_editor["ok"] is True
    assert shared_viewer["ok"] is True
    assert shared_viewer["results"][0]["mode"] == "viewer"

    editor_add = await purchase_update(
        _ctx(editor.id),
        [_op("add", name="масло", quantity=1, list_id=list_id)],
    )
    assert editor_add["ok"] is True
    assert editor_add["results"][0]["purchase_list"]["id"] == list_id

    viewer_view = await purchase_query(_ctx(viewer.id), store="spar", status="all")
    viewer_lists = [plist for plist in viewer_view["lists"] if plist["id"] == list_id]
    assert len(viewer_lists) == 1
    assert viewer_lists[0]["access"] == "viewer"
    assert {item["name"] for item in viewer_lists[0]["items"]} == {
        "хлеб",
        "молоко",
        "масло",
    }
    viewer_mutation = await purchase_update(
        _ctx(viewer.id), [_op("set", item_id=milk["id"], quantity=2)]
    )
    assert viewer_mutation["results"][0]["error"] == "denied"
    assert viewer_mutation["results"][0]["item"] is None

    assigned_view = await purchase_query(
        _ctx(assignee.id), store="spar", status="all", assigned_only=True
    )
    assigned_items = [
        item for plist in assigned_view["lists"] for item in plist["items"]
    ]
    assert [item["id"] for item in assigned_items] == [bread["id"]]
    assert assigned_items[0]["assignee_name"] == assignee.display_name

    # Removal is owner/editor-only even for the assignee's own pending row.
    assignee_remove = await purchase_update(
        _ctx(assignee.id), [_op("remove", item_id=bread["id"])]
    )
    remove_row = assignee_remove["results"][0]
    assert remove_row["ok"] is False
    assert remove_row["error"] == "denied"
    assert remove_row["item"] is None

    assignee_bought = await purchase_update(
        _ctx(assignee.id), [_op("mark_bought", item_id=bread["id"])]
    )
    assert assignee_bought["ok"] is True
    assert assignee_bought["results"][0]["item"]["status"] == "bought"

    editor_share = await purchase_share(
        _ctx(editor.id), [viewer.display_name], store="spar"
    )
    assert editor_share["error"] == "denied"

    owner_view = await purchase_query(_ctx(owner.id), store="spar", status="all")
    owner_items = {
        item["name"]: item for plist in owner_view["lists"] for item in plist["items"]
    }
    assert owner_items["хлеб"]["status"] == "bought"
    assert owner_items["масло"]["status"] == "pending"
    assert owner_items["молоко"]["quantity"] == 1
