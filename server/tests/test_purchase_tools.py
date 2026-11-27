# ruff: noqa: RUF001
"""Agent purchase tools: registration, batching, targeting, sharing, output."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from concierge.agent.tools import SUBAGENT_EXCLUDED, ToolDeps, allowed_tool_names
from concierge.core import db
from concierge.purchases import service
from concierge.purchases.tools import (
    PurchaseOperation,
    purchase_query,
    purchase_share,
    purchase_store,
    purchase_update,
)
from tests.conftest import ROLE_SEEDS, make_user


def _ctx(user_id: int, trigger_text: str = "") -> Any:
    return SimpleNamespace(
        deps=ToolDeps(
            user_id=user_id, conversation_id=0, trigger_text=trigger_text
        )
    )


def _op(action: str, **kwargs: Any) -> PurchaseOperation:
    return PurchaseOperation.model_validate({"action": action, **kwargs})


def test_tools_registered_and_gated() -> None:
    expected = {
        "purchase_update",
        "purchase_query",
        "purchase_store",
        "purchase_share",
    }
    for role in ("adult", "child", "speaker"):
        assert expected <= set(allowed_tool_names(ROLE_SEEDS[role]))
    assert not expected & set(allowed_tool_names(ROLE_SEEDS["guest"]))
    # If an administrator deliberately removes the guest tool deny-list, the
    # tools become callable, while purchase-domain list permissions still apply.
    reconfigured_guest = {"tools": {"allow": ["*"], "deny": []}}
    assert expected <= set(allowed_tool_names(reconfigured_guest))


async def test_purchase_update_tool_add_and_query(engine) -> None:
    user = await make_user("pt_add", "adult")
    ctx = _ctx(user.id)
    result = await purchase_update(
        ctx,
        [
            _op(
                "add",
                name="Молоко",
                quantity=2,
                unit="литра",
                category="молочные",
                price=80,
                price_kind="unit",
                general=True,
            )
        ],
    )
    assert result["ok"] is True
    row = result["results"][0]
    assert row["ok"] is True and row["item"]["name"] == "Молоко"
    assert row["item"]["quantity"] == 2
    assert row["item"]["unit"] == "л"
    assert row["item"]["price"] == 80
    assert row["item"]["price_kind"] == "unit"

    query = await purchase_query(ctx, general=True)
    assert query["lists"]
    item = query["lists"][0]["items"][0]
    assert item["id"] == row["item"]["id"]
    assert item["category"] == "молочные"
    assert query["markdown"].startswith("# Покупки")
    assert query["spoken_text"] == ctx.deps.spoken_text


async def test_unspecified_duplicate_add_noops_through_tool_schema(engine) -> None:
    user = await make_user("pt_unspecified", "adult")
    ctx = _ctx(user.id)
    first = await purchase_update(
        ctx, [_op("add", name="яблоки", quantity=2, unit="кг", general=True)]
    )
    first_item = first["results"][0]["item"]
    duplicate = await purchase_update(
        ctx, [_op("add", name="яблоки", general=True)]
    )
    assert duplicate["ok"] is True
    duplicate_item = duplicate["results"][0]["item"]
    assert duplicate_item["id"] == first_item["id"]
    assert duplicate_item["quantity"] == 2
    assert duplicate_item["unit"] == "кг"

    priced = await purchase_update(
        _ctx(user.id),
        [_op("add", name="яблоки", general=True, price=80, price_kind="unit")],
    )
    priced_item = priced["results"][0]["item"]
    assert priced_item["id"] == first_item["id"]
    assert priced_item["quantity"] == 2
    assert priced_item["unit"] == "кг"
    assert priced_item["price"] == 80
    assert priced_item["price_kind"] == "unit"

    explicit_piece = await purchase_update(
        ctx, [_op("add", name="яблоки", quantity=1, general=True)]
    )
    assert explicit_piece["ok"] is False
    mismatch = explicit_piece["results"][0]
    assert mismatch["error"] == "unit_mismatch"
    assert mismatch["item"]["id"] == first_item["id"]
    assert [candidate["item_id"] for candidate in mismatch["candidates"]] == [
        first_item["id"]
    ]
    assert mismatch["next_action"] == "ask_user_question"
    assert "количество" in mismatch["suggested_question"]["question"]
    labels = [option["label"] for option in mismatch["suggested_question"]["options"]]
    assert labels == ["Скорректировать существующую", "Добавить отдельной строкой"]
    query = await purchase_query(ctx, general=True)
    rows = query["lists"][0]["items"]
    assert len(rows) == 1


async def test_allow_incompatible_unit_opt_in_through_tool_schema(engine) -> None:
    user = await make_user("pt_dims_opt_in", "adult")
    ctx = _ctx(user.id)
    first = await purchase_update(
        ctx, [_op("add", name="яблоки", quantity=2, unit="кг", general=True)]
    )
    first_item = first["results"][0]["item"]

    rejected = await purchase_update(
        _ctx(user.id),
        [_op("add", name="яблоки", quantity=1, unit="шт", general=True)],
    )
    assert rejected["results"][0]["error"] == "unit_mismatch"

    separate = await purchase_update(
        ctx,
        [
            _op(
                "add",
                name="яблоки",
                quantity=1,
                unit="шт",
                general=True,
                allow_incompatible_unit=True,
            )
        ],
    )
    separate_row = separate["results"][0]
    assert separate_row["ok"] is True
    assert separate_row["item"]["id"] != first_item["id"]
    assert separate_row["item"]["unit"] == "шт"

    merged = await purchase_update(
        ctx,
        [
            _op(
                "add",
                name="яблоки",
                quantity=500,
                unit="г",
                general=True,
                allow_incompatible_unit=True,
            )
        ],
    )
    merged_row = merged["results"][0]
    assert merged_row["ok"] is True
    assert merged_row["item"]["id"] == first_item["id"]
    assert merged_row["item"]["quantity"] == 2.5
    assert merged_row["item"]["unit"] == "кг"

    query = await purchase_query(ctx, general=True)
    rows = query["lists"][0]["items"]
    assert len(rows) == 2
    assert {row["unit"] for row in rows} == {"кг", "шт"}


async def test_exact_batch_replay_is_cached_noop(engine) -> None:
    user = await make_user("pt_replay", "adult")
    ctx = _ctx(user.id)
    operations = [
        _op("add", name="сыр", quantity=1, unit="шт", general=True),
        _op("add", name="сыр", quantity=1, unit="шт", general=True),
    ]
    first = await purchase_update(ctx, operations)
    assert first["ok"] is True
    # Repeated operations inside one legitimate batch are real batch operations:
    # they merge once to 2 and must not be mistaken for a cross-batch replay.
    assert first["results"][-1]["item"]["quantity"] == 2

    replay = await purchase_update(ctx, operations)
    assert replay["ok"] is True
    assert replay["deduplicated"] is True
    assert replay["purchase_update_count"] == 2
    assert replay["results"] == first["results"]
    query = await purchase_query(ctx, general=True)
    rows = query["lists"][0]["items"]
    assert len(rows) == 1
    assert rows[0]["quantity"] == 2


async def test_parallel_exact_batch_replay_is_serialized(engine) -> None:
    user = await make_user("pt_parallel_replay", "adult")
    ctx = _ctx(user.id)
    operations = [_op("add", name="кефир", quantity=1, general=True)]
    first, second = await asyncio.gather(
        purchase_update(ctx, operations), purchase_update(ctx, operations)
    )
    assert first["ok"] is True and second["ok"] is True
    assert [bool(row.get("deduplicated")) for row in (first, second)].count(True) == 1
    query = await purchase_query(ctx, general=True)
    rows = query["lists"][0]["items"]
    assert len(rows) == 1
    assert rows[0]["quantity"] == 1


async def test_purchase_update_run_circuit_breaker(engine) -> None:
    user = await make_user("pt_breaker", "adult")
    ctx = _ctx(user.id)
    for name in ("чай", "кофе", "какао"):
        result = await purchase_update(ctx, [_op("add", name=name, general=True)])
        assert result["ok"] is True
    blocked = await purchase_update(ctx, [_op("add", name="сахар", general=True)])
    assert blocked["ok"] is False
    assert blocked["error"] == "purchase_update_loop_detected"
    assert blocked["purchase_update_count"] == 4
    assert blocked["purchase_update_limit"] == 3
    assert blocked["terminal"] is True
    assert "do not call purchase_update again" in blocked["instruction"].lower()
    query = await purchase_query(ctx, general=True)
    names = {item["name"] for item in query["lists"][0]["items"]}
    assert names == {"чай", "кофе", "какао"}

    # Separate user messages get fresh per-run deps and are unaffected.
    separate = await purchase_update(
        _ctx(user.id), [_op("add", name="сахар", general=True)]
    )
    assert separate["ok"] is True


async def test_trigger_factor_fallback_multiply_validation_phrase(engine) -> None:
    user = await make_user("pt_factor_multiply", "adult")
    added = await purchase_update(
        _ctx(user.id),
        [_op("add", name="молоко", quantity=2, unit="л", general=True)],
    )
    assert added["ok"] is True

    ctx = _ctx(user.id, "сделай молока в полтора раза больше")
    result = await purchase_update(
        ctx, [_op("multiply", name="молоко", general=True)]
    )
    assert result["ok"] is True
    assert result["results"][0]["item"]["quantity"] == 3


async def test_trigger_factor_fallback_divide_less_phrases(engine) -> None:
    user = await make_user("pt_factor_divide", "adult")
    await purchase_update(
        _ctx(user.id),
        [
            _op("add", name="молоко", quantity=3, unit="л", general=True),
            _op("add", name="сок", quantity=4, unit="л", general=True),
        ],
    )

    worded = await purchase_update(
        _ctx(user.id, "сделай молока в полтора раза меньше"),
        [_op("divide", name="молоко", general=True)],
    )
    assert worded["ok"] is True
    assert worded["results"][0]["item"]["quantity"] == 2

    numeric = await purchase_update(
        _ctx(user.id, "сделай сок в 2 раза меньше"),
        [_op("divide", name="сок", general=True)],
    )
    assert numeric["ok"] is True
    assert numeric["results"][0]["item"]["quantity"] == 2


async def test_trigger_factor_fallback_never_overrides_explicit_factor(engine) -> None:
    user = await make_user("pt_factor_explicit", "adult")
    await purchase_update(
        _ctx(user.id),
        [_op("add", name="молоко", quantity=2, unit="л", general=True)],
    )
    result = await purchase_update(
        _ctx(user.id, "сделай молока в полтора раза больше"),
        [_op("multiply", name="молоко", factor=2, general=True)],
    )
    assert result["ok"] is True
    assert result["results"][0]["item"]["quantity"] == 4


async def test_trigger_factor_fallback_fail_closed_for_compound_operations(
    engine,
) -> None:
    user = await make_user("pt_factor_compound", "adult")
    await purchase_update(
        _ctx(user.id),
        [
            _op("add", name="молоко", quantity=2, unit="л", general=True),
            _op("add", name="хлеб", quantity=1, unit="шт", general=True),
        ],
    )

    result = await purchase_update(
        _ctx(user.id, "сделай молоко в два раза больше и хлеб в три раза больше"),
        [
            _op("multiply", name="молоко", general=True),
            _op("multiply", name="хлеб", general=True),
        ],
    )
    assert [row["error"] for row in result["results"]] == [
        "factor_required",
        "factor_required",
    ]

    conflicting = await purchase_update(
        _ctx(user.id, "сделай молоко в два раза больше и в три раза больше"),
        [_op("multiply", name="молоко", general=True)],
    )
    assert conflicting["results"][0]["error"] == "factor_required"
    unsupported_conflict = await purchase_update(
        _ctx(user.id, "сделай молоко в два раза больше и в четыре раза больше"),
        [_op("multiply", name="молоко", general=True)],
    )
    assert unsupported_conflict["results"][0]["error"] == "factor_required"

    query = await purchase_query(_ctx(user.id), general=True)
    quantities = {
        item["name"]: item["quantity"] for item in query["lists"][0]["items"]
    }
    assert quantities == {"молоко": 2, "хлеб": 1}


async def test_trigger_assignment_and_store_validation_phrase(engine) -> None:
    owner = await make_user("pt_fallback_owner", "adult")
    assignee = await make_user("тихон", "adult")
    ctx = _ctx(
        owner.id,
        "Тихон должен купить килограмм яблок и литр молока в Пятёрочке",
    )
    result = await purchase_update(
        ctx,
        [
            _op("add", name="яблоки", quantity=1, unit="кг"),
            _op("add", name="молоко", quantity=1, unit="л"),
        ],
    )
    assert result["ok"] is True
    assert {row["item"]["assigned_to"] for row in result["results"]} == {
        assignee.id
    }
    assert {row["purchase_list"]["store_key"] for row in result["results"]} == {
        "pyaterochka"
    }
    assert {row["purchase_list"]["store_name"] for row in result["results"]} == {
        "Пятёрочка"
    }


async def test_trigger_fallback_preserves_explicit_assignee_and_store(engine) -> None:
    owner = await make_user("pt_fallback_explicit", "adult")
    await make_user("тихон_explicit", "adult")
    nastya = await make_user("настя_explicit", "adult")
    trigger = "Тихон_explicit должен купить хлеб и сыр в Пятёрочке"
    result = await purchase_update(
        _ctx(owner.id, trigger),
        [
            _op("add", name="хлеб", assignee="Настя_explicit", store="SPAR"),
            _op("add", name="сыр"),
        ],
    )
    assert result["ok"] is True
    bread, cheese = result["results"]
    assert bread["item"]["assigned_to"] == nastya.id
    assert bread["purchase_list"]["store_key"] == "spar"
    # One explicit field means the model already made a batch-level choice;
    # the fallback must not partially rewrite the remaining operation.
    assert cheese["item"]["assigned_to"] is None
    assert cheese["purchase_list"]["store_key"] == ""


async def test_trigger_fallback_skips_mixed_batch_and_general_store(engine) -> None:
    owner = await make_user("pt_fallback_mixed", "adult")
    await make_user("тихон_mixed", "adult")
    mixed = await purchase_update(
        _ctx(owner.id, "Тихон_mixed должен купить чай в Пятёрочке"),
        [
            _op("add", name="чай"),
            _op("set", name="несуществующий", quantity=1),
        ],
    )
    added = mixed["results"][0]
    assert added["ok"] is True
    assert added["item"]["assigned_to"] is None
    assert added["purchase_list"]["store_key"] == ""

    general = await purchase_update(
        _ctx(owner.id, "купить кофе в пятерочке"),
        [_op("add", name="кофе", general=True)],
    )
    row = general["results"][0]
    assert row["ok"] is True
    assert row["purchase_list"]["store_key"] == ""


async def test_trigger_fallback_normalizes_before_replay_cache(engine) -> None:
    owner = await make_user("pt_fallback_replay", "adult")
    assignee = await make_user("тихон", "adult")
    ctx = _ctx(
        owner.id,
        "Тихон должен купить килограмм яблок и литр молока в Пятёрочке",
    )
    operations = [
        _op("add", name="яблоки", quantity=1, unit="кг"),
        _op("add", name="молоко", quantity=1, unit="л"),
    ]
    first = await purchase_update(ctx, operations)
    replay = await purchase_update(ctx, operations)
    assert first["ok"] is True
    assert replay["deduplicated"] is True
    assert replay["results"] == first["results"]

    query = await purchase_query(ctx, store="Пятёрочка", status="all")
    rows = query["lists"][0]["items"]
    assert len(rows) == 2
    assert {row["quantity"] for row in rows} == {1}
    assert {row["assigned_to"] for row in rows} == {assignee.id}


async def test_fallback_category_inference_and_explicit_category(engine) -> None:
    user = await make_user("pt_categories", "adult")
    ctx = _ctx(user.id)
    result = await purchase_update(
        ctx,
        [
            _op("add", name="яблоки", general=True),
            _op("add", name="молоко", general=True),
            _op("add", name="вода", general=True),
            _op("add", name="хлеб", general=True),
            _op("add", name="зубная паста", general=True),
            _op("add", name="бананы", category="прочее", general=True),
            _op("add", name="неочевидная вещь", general=True),
        ],
    )
    assert result["ok"] is True
    categories = {
        row["item"]["name"]: row["item"]["category"] for row in result["results"]
    }
    assert categories == {
        "яблоки": "овощи/фрукты",
        "молоко": "молочные",
        "вода": "напитки",
        "хлеб": "хлеб/выпечка",
        "зубная паста": "бытовая химия",
        "бананы": "прочее",
        "неочевидная вещь": "прочее",
    }


async def test_ambiguous_item_result_includes_question_scaffold(engine) -> None:
    user = await make_user("pt_item_scaffold", "adult")
    first = await purchase_update(
        _ctx(user.id), [_op("add", name="Молоко", quantity=1, unit="бутылка", general=True)]
    )
    second = await purchase_update(
        _ctx(user.id), [_op("add", name="Молоко", quantity=1, unit="упаковка", general=True)]
    )
    assert first["ok"] is True and second["ok"] is True
    ambiguous = await purchase_update(
        _ctx(user.id), [_op("remove", name="молоко", general=True)]
    )
    row = ambiguous["results"][0]
    assert row["error"] == "ambiguous_item"
    assert row["next_action"] == "ask_user_question"
    assert row["suggested_question"]["question"] == "Уточните позицию:"
    labels = [opt["label"] for opt in row["suggested_question"]["options"]]
    assert labels == ["Молоко — 1 бутылка", "Молоко — 1 упаковка"]
    assert [candidate["quantity"] for candidate in row["candidates"]] == [1, 1]


async def test_purchase_update_batch_correction(engine) -> None:
    user = await make_user("pt_batch", "adult")
    ctx = _ctx(user.id)
    added = await purchase_update(
        ctx, [_op("add", name="Яблоки", quantity=4, unit="шт", general=True)]
    )
    assert added["ok"] is True
    result = await purchase_update(
        ctx,
        [
            _op("set", name="яблоки", quantity=2, general=True),
            _op("increase", name="яблоки", quantity=3, general=True),
            _op("decrease", name="яблоки", quantity=1, general=True),
            _op("multiply", name="яблоки", factor=2, general=True),
            _op("divide", name="яблоки", factor=4, general=True),
        ],
    )
    assert result["ok"] is True, result
    assert [row["ok"] for row in result["results"]] == [True] * 5
    assert result["results"][-1]["item"]["quantity"] == 2
    removed = await purchase_update(
        _ctx(user.id), [_op("remove", name="яблоки", general=True)]
    )
    assert removed["ok"] is True
    missing = await purchase_update(
        _ctx(user.id), [_op("remove", name="яблоки", general=True)]
    )
    assert missing["ok"] is False
    assert missing["results"][0]["error"] == "not_found"


async def test_item_id_mark_bought_and_pending(engine) -> None:
    user = await make_user("pt_ids", "adult")
    ctx = _ctx(user.id)
    added = await purchase_update(
        ctx, [_op("add", name="Хлеб", quantity=1, general=True)]
    )
    item_id = added["results"][0]["item"]["id"]
    bought = await purchase_update(ctx, [_op("mark_bought", item_id=item_id)])
    assert bought["ok"] is True
    assert bought["results"][0]["item"]["status"] == "bought"
    pending = await purchase_update(ctx, [_op("mark_pending", item_id=item_id)])
    assert pending["ok"] is True
    assert pending["results"][0]["item"]["status"] == "pending"
    stale = await purchase_update(
        _ctx(user.id), [_op("mark_bought", item_id=item_id + 1000)]
    )
    assert stale["ok"] is False
    assert stale["results"][0]["error"] == "not_found"


async def test_unknown_bought_uses_store_context(engine) -> None:
    user = await make_user("pt_contextbuy", "adult")
    ctx = _ctx(user.id)
    await purchase_store(ctx, action="set", store="спар")
    result = await purchase_update(ctx, [_op("mark_bought", name="Кофе", quantity=1)])
    assert result["ok"] is True
    assert result["results"][0]["purchase_list"]["store_key"] == "spar"
    query = await purchase_query(ctx, status="bought")
    assert any(
        item["name"] == "Кофе"
        for plist in query["lists"]
        for item in plist["items"]
    )
    assert query["store_context"]["store_key"] == "spar"


async def test_purchase_store_set_clear_current(engine) -> None:
    user = await make_user("pt_store", "adult")
    ctx = _ctx(user.id)
    empty = await purchase_store(ctx, action="current")
    assert empty["ok"] is True and empty["store_context"] is None
    missing = await purchase_store(ctx, action="set")
    assert missing["error"] == "store_required"
    set_result = await purchase_store(ctx, action="set", store="пятёрочка")
    assert set_result["ok"] is True
    assert set_result["store_context"]["store_key"] == "pyaterochka"
    assert set_result["store_context"]["store_name"] == "Пятёрочка"
    current = await purchase_store(ctx, action="current")
    assert current["store_context"]["store_key"] == "pyaterochka"
    cleared = await purchase_store(ctx, action="clear")
    assert cleared["ok"] is True and cleared["cleared"] is True
    assert (await purchase_store(ctx, action="current"))["store_context"] is None


async def test_share_tool_requires_owner(engine) -> None:
    owner = await make_user("pt_share_owner", "adult")
    editor = await make_user("pt_share_editor", "adult")
    third = await make_user("pt_share_third", "adult")
    owner_ctx = _ctx(owner.id)
    added = await purchase_update(
        owner_ctx, [_op("add", name="Чай", store="спар")]
    )
    assert added["ok"] is True
    shared = await purchase_share(owner_ctx, [editor.display_name], store="спар")
    assert shared["ok"] is True
    assert shared["results"][0]["mode"] == "editor"

    # The editor can mutate the shared list, but cannot re-share it: ownership
    # stays with the list owner even though the tool resolved the same store.
    denied = await purchase_share(
        _ctx(editor.id), [third.display_name], store="спар"
    )
    assert denied["error"] == "denied"


async def test_share_resolve_ambiguous_user(engine) -> None:
    owner = await make_user("pt_amb_owner", "adult")
    await make_user("nastya_a", "adult")
    await make_user("nastya_b", "adult")
    result = await purchase_share(_ctx(owner.id), ["nastya"], general=True)
    row = result["results"][0]
    assert row["ok"] is False
    assert row["error"] == "ambiguous_user"
    assert len(row["candidates"]) == 2
    assert row["next_action"] == "ask_user_question"
    assert row["suggested_question"]["question"] == "Уточните пользователя:"
    labels = [opt["label"] for opt in row["suggested_question"]["options"]]
    assert labels == [candidate["name"] for candidate in row["candidates"]]


async def test_assignment_visible_to_assignee(engine) -> None:
    owner = await make_user("pt_assign_owner", "adult")
    guest = await make_user("pt_assign_guest", "guest")
    added = await purchase_update(
        _ctx(owner.id),
        [
            _op(
                "add",
                name="Авокадо",
                quantity=2,
                assignee=guest.display_name,
                general=True,
            ),
            _op("add", name="Крупа", quantity=1, general=True),
        ],
    )
    assert added["ok"] is True, added
    view = await purchase_query(_ctx(guest.id), general=True)
    assert view["lists"]
    assert view["lists"][0]["access"] == "assignee"
    assert [item["name"] for item in view["lists"][0]["items"]] == ["Авокадо"]
    assert view["lists"][0]["items"][0]["assignee_name"] == guest.display_name


async def test_query_returns_markdown_and_spoken(engine) -> None:
    user = await make_user("pt_render", "adult")
    ctx = _ctx(user.id)
    result = await purchase_update(
        ctx,
        [
            _op("add", name="яблоки", quantity=2, unit="кг", store="Пятёрочка"),
            _op("add", name="хлеб", quantity=1, store="Пятёрочка"),
        ],
    )
    item_id = result["results"][1]["item"]["id"]
    await purchase_update(ctx, [_op("mark_bought", item_id=item_id)])
    query = await purchase_query(ctx, store="пятёрочка", status="all")
    assert "## Купить в Пятёрочка" in query["markdown"]
    assert "- [ ] 2 кг яблоки" in query["markdown"]
    assert "- [x] 1 шт хлеб" in query["markdown"]
    assert "В Пятёрочке уже купили хлеб." in query["spoken_text"]
    assert "осталось купить 2 килограмма яблоки" in query["spoken_text"]
    assert ctx.deps.spoken_text == query["spoken_text"]


async def test_mutation_tools_excluded_from_subagents() -> None:
    assert "purchase_update" in SUBAGENT_EXCLUDED
    assert "purchase_share" in SUBAGENT_EXCLUDED
    assert "purchase_store" in SUBAGENT_EXCLUDED
    assert "purchase_query" not in SUBAGENT_EXCLUDED


async def test_guest_has_no_implicit_purchase_access_even_if_tool_allowed(engine) -> None:
    owner = await make_user("pt_guest_owner", "adult")
    guest = await make_user("pt_guest_reader", "guest")
    added = await purchase_update(
        _ctx(owner.id), [_op("add", name="Соль", general=True)]
    )
    assert added["ok"] is True
    view = await purchase_query(_ctx(guest.id), general=True, all_stores=True)
    assert not [
        item
        for plist in view["lists"]
        for item in plist["items"]
        if item["name"] == "Соль"
    ]
    async with db.get_sessionmaker()() as session:
        assert await service.query(session, guest, mode="family") is not None


async def test_trigger_store_fallback_covers_canonical_service_aliases(engine) -> None:
    user = await make_user("pt_store_aliases", "adult")
    cases = [
        ("купить молоко в Магните", "молоко", "magnit", "Магнит"),
        ("купить хлеб в Ленте", "хлеб", "lenta", "Лента"),
        ("купить сок в Ашане", "сок", "ashan", "Ашан"),
    ]
    for trigger, name, store_key, store_name in cases:
        result = await purchase_update(
            _ctx(user.id, trigger), [_op("add", name=name, quantity=1)]
        )
        row = result["results"][0]
        assert row["ok"] is True
        assert row["purchase_list"]["store_key"] == store_key
        assert row["purchase_list"]["store_name"] == store_name


async def test_trigger_store_fallback_fail_closed_for_mixed_stores(engine) -> None:
    user = await make_user("pt_store_mixed", "adult")
    result = await purchase_update(
        _ctx(user.id, "купить молоко в Пятёрочке и хлеб в Магните"),
        [_op("add", name="молоко"), _op("add", name="хлеб")],
    )
    assert result["ok"] is True
    assert [row["purchase_list"]["store_key"] for row in result["results"]] == [
        "",
        "",
    ]


async def test_trigger_assignee_fallback_requires_primary_intent(engine) -> None:
    owner = await make_user("pt_assign_primary", "adult")
    await make_user("тихон", "adult")
    result = await purchase_update(
        _ctx(owner.id, "надо купить хлеб, а Тихон должен купить молоко"),
        [_op("add", name="хлеб"), _op("add", name="молоко")],
    )
    assert result["ok"] is True
    assert [row["item"]["assigned_to"] for row in result["results"]] == [None, None]


async def test_trigger_assignee_fallback_fail_closed_for_contrasting_clauses(
    engine,
) -> None:
    owner = await make_user("pt_assign_contrast_owner", "adult")
    await make_user("тихон", "adult")
    await make_user("настя", "adult")
    cases = [
        (
            "Тихон должен купить яблоки, а я куплю молоко",
            "яблоки",
            "молоко",
        ),
        (
            "Тихон должен купить груши, а Настя должна купить кефир",
            "груши",
            "кефир",
        ),
    ]
    for trigger, first_name, second_name in cases:
        result = await purchase_update(
            _ctx(owner.id, trigger),
            [_op("add", name=first_name), _op("add", name=second_name)],
        )
        assert result["ok"] is True
        assert [row["item"]["assigned_to"] for row in result["results"]] == [
            None,
            None,
        ]


async def test_unspecified_duplicate_add_with_multiple_rows_is_ambiguous(engine) -> None:
    user = await make_user("pt_unspecified_ambiguous", "adult")
    ctx = _ctx(user.id)
    first = await purchase_update(
        ctx, [_op("add", name="Молоко", quantity=1, unit="бутылка", general=True)]
    )
    second = await purchase_update(
        _ctx(user.id), [_op("add", name="Молоко", quantity=1, unit="упаковка", general=True)]
    )
    assert first["ok"] is True and second["ok"] is True

    duplicate = await purchase_update(
        _ctx(user.id), [_op("add", name="молоко", general=True)]
    )
    row = duplicate["results"][0]
    assert row["ok"] is False
    assert row["error"] == "ambiguous_item"
    assert len(row["candidates"]) == 2
    assert row["next_action"] == "ask_user_question"
    query = await purchase_query(ctx, general=True)
    assert len(query["lists"][0]["items"]) == 2


async def test_purchase_update_bounds_errors_are_structured(engine) -> None:
    user = await make_user("pt_bounds", "adult")
    result = await purchase_update(
        _ctx(user.id),
        [
            _op("add", name="молоко", quantity=10_000_000_000, general=True),
            _op("add", name="хлеб", price=100_000_000_000, general=True),
        ],
    )
    assert result["ok"] is False
    assert [row["error"] for row in result["results"]] == [
        "invalid_quantity",
        "invalid_quantity",
    ]
    query = await purchase_query(_ctx(user.id), general=True, status="all")
    assert query["lists"] == []


async def test_assigned_fallback_ambiguity_includes_cross_list_item_candidates(
    engine,
) -> None:
    owner = await make_user("pt_assign_amb_owner", "adult")
    other_owner = await make_user("pt_assign_amb_other_owner", "adult")
    assignee = await make_user("pt_assign_amb_tikhon", "adult")
    today = service.family_today()
    async with db.get_sessionmaker()() as session:
        own = await service.add(session, assignee, "хлеб", 1, store="Пятёрочка")
        first = await service.add(
            session,
            owner,
            "молоко",
            1,
            store="Пятёрочка",
            planned_for=today,
            assigned_to=assignee.id,
        )
        second = await service.add(
            session,
            other_owner,
            "молоко",
            2,
            store="Пятёрочка",
            planned_for=today + timedelta(days=1),
            assigned_to=assignee.id,
        )
        assert own.ok and first.ok and second.ok
        assert first.item is not None and second.item is not None
        expected_ids = {first.item.id, second.item.id}
        expected_dates = {
            first.item.planned_for.isoformat(),
            second.item.planned_for.isoformat(),
        }

    result = await purchase_update(
        _ctx(assignee.id),
        [_op("set", name="молоко", quantity=3, store="Пятёрочка")],
    )
    row = result["results"][0]
    assert row["ok"] is False
    assert row["error"] == "ambiguous_item"
    assert len(row["candidates"]) == 2
    assert {candidate["item_id"] for candidate in row["candidates"]} == expected_ids
    assert {candidate["planned_for"] for candidate in row["candidates"]} == expected_dates
    assert {candidate["owner_name"] for candidate in row["candidates"]} == {
        owner.display_name,
        other_owner.display_name,
    }
    descriptions = [
        option["description"] for option in row["suggested_question"]["options"]
    ]
    assert any(owner.display_name in description for description in descriptions)
    assert any(other_owner.display_name in description for description in descriptions)
    assert row["next_action"] == "ask_user_question"
    assert len(row["suggested_question"]["options"]) == 2


async def test_purchase_update_overlong_unit_is_structured_per_operation(engine) -> None:
    user = await make_user("pt_unit_bounds", "adult")
    custom33 = "x" * 33
    result = await purchase_update(
        _ctx(user.id),
        [
            _op("add", name="сахар", quantity=1, general=True),
            _op("add", name="кабель", quantity=1, unit=custom33, general=True),
        ],
    )
    assert result["ok"] is False
    assert result["results"][0]["ok"] is True
    assert result["results"][1]["ok"] is False
    assert result["results"][1]["error"] == "invalid_unit"


async def test_purchase_query_assigned_only_filters_to_caller_assignments(engine) -> None:
    owner = await make_user("pt_query_owner", "adult")
    assignee = await make_user("pt_query_assignee", "adult")
    async with db.get_sessionmaker()() as session:
        assigned = await service.add(
            session, owner, "молоко", 1, general=True, assigned_to=assignee.id
        )
        unassigned = await service.add(session, owner, "хлеб", 1, general=True)
        assert assigned.ok and unassigned.ok

    assigned_view = await purchase_query(
        _ctx(assignee.id), general=True, assigned_only=True
    )
    assigned_names = {
        item["name"] for plist in assigned_view["lists"] for item in plist["items"]
    }
    assert assigned_view["assigned_only"] is True
    assert assigned_names == {"молоко"}

    default_view = await purchase_query(_ctx(assignee.id), general=True)
    default_names = {
        item["name"] for plist in default_view["lists"] for item in plist["items"]
    }
    assert default_view["assigned_only"] is False
    assert default_names == {"молоко", "хлеб"}


async def test_exact_item_id_state_transitions_are_idempotent(engine) -> None:
    user = await make_user("pt_idempotent_states", "adult")
    added = await purchase_update(
        _ctx(user.id), [_op("add", name="молоко", quantity=1, general=True)]
    )
    item_id = added["results"][0]["item"]["id"]

    first_bought = await purchase_update(
        _ctx(user.id), [_op("mark_bought", item_id=item_id)]
    )
    second_bought = await purchase_update(
        _ctx(user.id), [_op("mark_bought", item_id=item_id)]
    )
    assert first_bought["ok"] is True and second_bought["ok"] is True
    first_row = first_bought["results"][0]["item"]
    second_row = second_bought["results"][0]["item"]
    assert second_row["id"] == item_id
    assert second_row["status"] == "bought"
    assert second_row["bought_at"] == first_row["bought_at"]

    first_pending = await purchase_update(
        _ctx(user.id), [_op("mark_pending", item_id=item_id)]
    )
    second_pending = await purchase_update(
        _ctx(user.id), [_op("mark_pending", item_id=item_id)]
    )
    assert first_pending["ok"] is True and second_pending["ok"] is True
    assert second_pending["results"][0]["item"]["status"] == "pending"


async def test_tool_factor_results_match_persisted_quantity_scale(engine) -> None:
    user = await make_user("pt_quantity_scale", "adult")
    added = await purchase_update(
        _ctx(user.id), [_op("add", name="вода", quantity=1, general=True)]
    )
    item_id = added["results"][0]["item"]["id"]
    divided = await purchase_update(
        _ctx(user.id), [_op("divide", item_id=item_id, factor=3)]
    )
    assert divided["ok"] is True
    assert divided["results"][0]["item"]["quantity"] == 0.333

    sugar = await purchase_update(
        _ctx(user.id), [_op("add", name="сахар", quantity=1.234, general=True)]
    )
    sugar_id = sugar["results"][0]["item"]["id"]
    multiplied = await purchase_update(
        _ctx(user.id), [_op("multiply", item_id=sugar_id, factor=1.25)]
    )
    assert multiplied["ok"] is True
    assert multiplied["results"][0]["item"]["quantity"] == 1.543


async def test_denied_exact_item_mutations_do_not_leak_item_details(engine) -> None:
    owner = await make_user("pt_secret_owner", "adult")
    other_owner = await make_user("pt_decoy_owner", "adult")
    assignee = await make_user("pt_secret_assignee", "guest")
    viewer = await make_user("pt_secret_viewer", "guest")
    guest = await make_user("pt_secret_guest", "guest")
    async with db.get_sessionmaker()() as session:
        secret = await service.add(
            session,
            owner,
            "секретный товар",
            3,
            general=True,
            price_amount=999,
            price_kind="total",
        )
        assigned = await service.add(
            session,
            owner,
            "назначенный хлеб",
            1,
            general=True,
            assigned_to=assignee.id,
        )
        decoy = await service.add(session, other_owner, "публичный чай", 1, general=True)
        assert secret.ok and secret.item is not None
        assert assigned.ok and decoy.ok and decoy.purchase_list is not None
        secret_id = secret.item.id
        await service.share_list(
            session, other_owner, decoy.purchase_list.id, viewer.id, "viewer"
        )

        direct = await service.set_quantity(session, guest, secret_id, 5)
        assert not direct.ok and direct.error == "denied"
        assert direct.item is None and direct.purchase_list is None

    for probe in (assignee, viewer, guest):
        result = await purchase_update(
            _ctx(probe.id), [_op("set", item_id=secret_id, quantity=5)]
        )
        row = result["results"][0]
        assert row["ok"] is False
        assert row["error"] == "denied"
        assert row["item"] is None
        assert row["purchase_list"] is None
        assert row["candidates"] == []
        assert "секретный товар" not in str(row)
        assert "999" not in str(row)


async def test_ambiguous_list_can_be_retried_with_exact_list_id(engine) -> None:
    owner_a = await make_user("pt_exact_list_owner_a", "adult")
    owner_b = await make_user("pt_exact_list_owner_b", "adult")
    editor = await make_user("pt_exact_list_editor", "child")
    guest = await make_user("pt_exact_list_guest", "guest")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, owner_a, "хлеб", 1, store="spar")
        second = await service.add(session, owner_b, "хлеб", 1, store="spar")
        assert first.ok and first.purchase_list is not None
        assert second.ok and second.purchase_list is not None
        await service.share_list(session, owner_a, first.purchase_list.id, editor.id)
        await service.share_list(session, owner_b, second.purchase_list.id, editor.id)

    ambiguous = await purchase_update(
        _ctx(editor.id), [_op("add", name="молоко", quantity=1, store="SPAR")]
    )
    row = ambiguous["results"][0]
    assert row["ok"] is False and row["error"] == "ambiguous"
    assert len(row["candidates"]) == 2
    assert all(candidate["kind"] == "list" for candidate in row["candidates"])
    chosen_list_id = row["candidates"][0]["list_id"]

    retried = await purchase_update(
        _ctx(editor.id),
        [
            _op(
                "add",
                name="молоко",
                quantity=1,
                store="SPAR",
                general=True,
                list_id=chosen_list_id,
            )
        ],
    )
    retry_row = retried["results"][0]
    assert retry_row["ok"] is True
    assert retry_row["purchase_list"]["id"] == chosen_list_id
    assert retry_row["item"]["name"] == "молоко"

    denied = await purchase_update(
        _ctx(guest.id), [_op("add", name="сахар", list_id=chosen_list_id)]
    )
    denied_row = denied["results"][0]
    assert denied_row["ok"] is False
    assert denied_row["error"] == "denied"
    assert denied_row["item"] is None
    assert denied_row["purchase_list"] is None


async def test_purchase_update_batch_cap_rejects_before_any_mutation(engine) -> None:
    user = await make_user("pt_batch_cap", "adult")
    operations = [
        _op("add", name=f"товар {index}", quantity=1, general=True)
        for index in range(51)
    ]
    result = await purchase_update(_ctx(user.id), operations)
    assert result["ok"] is False
    assert result["error"] == "too_many_operations"
    assert result["operation_count"] == 51
    assert result["operation_limit"] == 50
    assert result["results"] == []

    query = await purchase_query(_ctx(user.id), general=True, status="all")
    assert query["lists"] == []
