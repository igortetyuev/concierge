# ruff: noqa: RUF001
"""Agent-facing purchase tools.

The language understanding stays in the calling model/skill; these tools are a
thin typed layer over the deterministic purchase service and always return
structured errors/candidates instead of leaking tracebacks.
"""

import asyncio
import copy
import json
import re
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from pydantic_ai import RunContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.core.db import get_sessionmaker
from concierge.purchases import categories, render, service
from concierge.purchases.models import PurchaseItem, PurchaseList
from concierge.purchases.units import practical_number


class PurchaseToolDeps(Protocol):
    """Structural subset of the agent's ToolDeps used by purchase tools."""

    user_id: int
    trigger_text: str
    spoken_text: str | None
    purchase_update_count: int
    purchase_update_results: dict[str, dict[str, Any]]
    purchase_update_lock: asyncio.Lock


class PurchaseOperation(BaseModel):
    """One purchase-list mutation; items are addressed by exact id or name."""

    action: Literal[
        "add",
        "set",
        "increase",
        "decrease",
        "multiply",
        "divide",
        "remove",
        "mark_bought",
        "mark_pending",
    ]
    item_id: int | None = None
    list_id: int | None = None
    name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    factor: float | None = None
    category: str | None = None
    store: str | None = None
    general: bool = False
    planned_for: date | None = None
    assignee: str | None = None
    price: float | None = None
    price_kind: Literal["unit", "total"] | None = None
    currency: str | None = None
    allow_incompatible_unit: bool = False


class PurchaseOperationResult(BaseModel):
    """Structured result of one operation in a `purchase_update` batch."""

    action: str
    ok: bool
    item: dict[str, Any] | None = None
    purchase_list: dict[str, Any] | None = None
    error: str | None = None
    candidates: list[dict[str, Any]] = []
    next_action: str | None = None
    suggested_question: dict[str, Any] | None = None


def _list_out(plist: PurchaseList | None) -> dict[str, Any] | None:
    if plist is None:
        return None
    title = "Покупки" if not plist.store_key else f"Купить в {plist.store_name or plist.store_key}"
    return {
        "id": plist.id,
        "owner_id": plist.owner_id,
        "store_key": plist.store_key,
        "store_name": plist.store_name,
        "title": title,
    }


def _item_out(item: PurchaseItem | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "list_id": item.list_id,
        "name": item.name,
        "quantity": practical_number(item.quantity) or 0,
        "unit": item.unit,
        "category": item.category,
        "status": item.status,
        "planned_for": item.planned_for.isoformat(),
        "price": practical_number(item.price_amount),
        "currency": item.price_currency,
        "price_kind": item.price_kind,
        "assigned_to": item.assigned_to,
        "bought_by": item.bought_by,
        "bought_at": item.bought_at.isoformat() if item.bought_at else None,
    }


def _list_candidates(candidates: list[service.ListRef]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "list",
            "list_id": row.id,
            "owner_id": row.owner_id,
            "owner_name": row.owner_name,
            "store_key": row.store_key,
            "store_name": row.store_name,
            "access": row.access,
        }
        for row in candidates
    ]


def _candidate_label(candidate: dict[str, Any]) -> str:
    kind = candidate.get("kind")
    if kind == "item":
        name = str(candidate.get("name") or "")
        quantity = candidate.get("quantity")
        unit = str(candidate.get("unit") or "")
        amount = ""
        if quantity is not None:
            amount = str(quantity)
            if unit:
                amount = f"{amount} {unit}"
        elif unit:
            amount = unit
        return f"{name} — {amount}" if amount else name
    if kind == "list":
        store = candidate.get("store_name") or candidate.get("store_key")
        return f"Купить в {store}" if store else "Покупки"
    return str(candidate.get("name") or "")


def _candidate_description(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    if candidate.get("kind") == "item":
        store = candidate.get("store_name") or candidate.get("store_key")
        if store:
            parts.append(str(store))
        if candidate.get("owner_name"):
            parts.append(str(candidate["owner_name"]))
        if candidate.get("planned_for"):
            parts.append(str(candidate["planned_for"]))
    elif candidate.get("kind") == "list" and candidate.get("owner_name"):
        parts.append(str(candidate["owner_name"]))
    return ", ".join(parts)


def _guidance_fields(
    error: str | None, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Machine-readable scaffold for the required ask_user_question follow-up."""
    if error not in {"ambiguous", "ambiguous_item", "ambiguous_user", "unit_mismatch"}:
        return {}
    questions = {
        "ambiguous": "Уточните список:",
        "ambiguous_item": "Уточните позицию:",
        "ambiguous_user": "Уточните пользователя:",
        "unit_mismatch": (
            "Уточните количество/единицу: скорректировать существующую позицию "
            "или добавить отдельную строку?"
        ),
    }
    options = []
    if error == "unit_mismatch" and candidates:
        existing = candidates[0]
        existing_label = _candidate_label(existing)
        options = [
            {
                "label": "Скорректировать существующую",
                "description": existing_label,
            },
            {
                "label": "Добавить отдельной строкой",
                "description": "Сохранить обе формы или единицы как разные позиции",
            },
        ]
    elif error != "unit_mismatch":
        options = [
            {
                "label": _candidate_label(candidate),
                "description": _candidate_description(candidate),
            }
            for candidate in candidates[:4]
            if _candidate_label(candidate)
        ]
    return {
        "next_action": "ask_user_question",
        "suggested_question": {
            "question": questions[error],
            "header": "Покупки",
            "options": options,
        },
    }


def _populate_guidance(out: PurchaseOperationResult) -> PurchaseOperationResult:
    fields = _guidance_fields(out.error, out.candidates)
    if fields:
        out.next_action = fields["next_action"]
        out.suggested_question = fields["suggested_question"]
    return out


def _operation_error(
    action: str,
    error: str,
    candidates: list[dict[str, Any]] | None = None,
) -> PurchaseOperationResult:
    return _populate_guidance(
        PurchaseOperationResult(
            action=action, ok=False, error=error, candidates=candidates or []
        )
    )


def _mutation_out(result: service.MutationResult) -> PurchaseOperationResult:
    denied = result.error == "denied"
    return PurchaseOperationResult(
        action="",
        ok=result.ok,
        item=None if denied else _item_out(result.item),
        purchase_list=None if denied else _list_out(result.purchase_list),
        error=result.error,
        candidates=[] if denied else _list_candidates(result.candidates),
    )


async def _resolve_user(
    session: AsyncSession, name: str
) -> tuple[User | None, list[str]]:
    """Reuse the exact invite/forward active-user resolution at runtime."""
    from concierge.agent.tools import _resolve_user as resolve

    return await resolve(session, name)


async def _assignee_id(
    session: AsyncSession, op: PurchaseOperation
) -> tuple[int | None, PurchaseOperationResult | None]:
    if op.assignee is None or not op.assignee.strip():
        return None, None
    user, candidates = await _resolve_user(session, op.assignee)
    if user is not None:
        return user.id, None
    error = "ambiguous_user" if candidates else "user_not_found"
    return None, _operation_error(
        op.action,
        error,
        [{"kind": "user", "name": name} for name in candidates],
    )


def _item_candidate(
    row: PurchaseItem,
    plist: PurchaseList | None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": "item",
        "item_id": row.id,
        "list_id": row.list_id,
        "name": row.name,
        "quantity": practical_number(row.quantity) or 0,
        "unit": row.unit,
        "status": row.status,
        "planned_for": row.planned_for.isoformat(),
        "store_key": plist.store_key if plist is not None else "",
        "store_name": plist.store_name if plist is not None else None,
        "owner_id": plist.owner_id if plist is not None else None,
        "owner_name": owner_name,
    }


async def _owner_names(
    session: AsyncSession, plists: list[PurchaseList | None]
) -> dict[int, str]:
    owner_ids = {plist.owner_id for plist in plists if plist is not None}
    if not owner_ids:
        return {}
    rows = (
        await session.execute(select(User).where(User.id.in_(owner_ids)))
    ).scalars()
    return {row.id: row.display_name for row in rows}


async def _item_candidates(
    session: AsyncSession,
    result: service.MutationResult,
    op: PurchaseOperation,
    status: str | None,
) -> list[dict[str, Any]]:
    """Concrete item choices for `ambiguous_item` results."""
    if op.name is None or not op.name.strip():
        return []
    rows: list[PurchaseItem] = []
    if result.purchase_list is not None:
        stmt = select(PurchaseItem).where(
            PurchaseItem.list_id == result.purchase_list.id,
            PurchaseItem.canonical_key == service.normalize_product_key(op.name),
        )
        if status is not None:
            stmt = stmt.where(PurchaseItem.status == status)
        rows = list((await session.execute(stmt.order_by(PurchaseItem.id))).scalars())
    if not rows:
        return []
    owners = {
        plist.id: plist
        for plist in (
            await session.execute(
                select(PurchaseList).where(PurchaseList.id.in_({row.list_id for row in rows}))
            )
        ).scalars()
    }
    owner_names = await _owner_names(session, list(owners.values()))
    candidates: list[dict[str, Any]] = []
    for row in rows:
        plist = owners.get(row.list_id)
        candidates.append(
            _item_candidate(
                row,
                plist,
                owner_names.get(plist.owner_id) if plist is not None else None,
            )
        )
    return candidates


async def _finish_mutation(
    session: AsyncSession,
    result: service.MutationResult,
    op: PurchaseOperation,
    status: str | None = None,
) -> PurchaseOperationResult:
    out = _mutation_out(result)
    out.action = op.action
    if result.error == "unit_mismatch":
        owner_names = await _owner_names(session, [result.purchase_list])
        out.candidates = (
            [
                _item_candidate(
                    result.item,
                    result.purchase_list,
                    owner_names.get(result.purchase_list.owner_id)
                    if result.purchase_list is not None
                    else None,
                )
            ]
            if result.item is not None
            else []
        )
    elif result.error == "ambiguous_item":
        if result.item_candidates:
            owner_names = await _owner_names(
                session, [plist for _item, plist in result.item_candidates]
            )
            out.candidates = [
                _item_candidate(item, plist, owner_names.get(plist.owner_id))
                for item, plist in result.item_candidates
            ]
        else:
            out.candidates = await _item_candidates(session, result, op, status)
    return _populate_guidance(out)


def _item_ref(op: PurchaseOperation) -> int | str | None:
    if op.item_id is not None:
        return op.item_id
    if op.name is not None and op.name.strip():
        return op.name
    return None


async def _apply_operation(
    session: AsyncSession, user_id: int, op: PurchaseOperation
) -> PurchaseOperationResult:
    if op.item_id is not None and op.item_id <= 0:
        return _operation_error(op.action, "invalid_item_id")
    if op.list_id is not None and op.list_id <= 0:
        return _operation_error(op.action, "invalid_list_id")
    item_ref = _item_ref(op)
    if item_ref is None:
        return _operation_error(op.action, "item_or_name_required")
    if op.action in ("set", "increase", "decrease") and op.quantity is None:
        return _operation_error(op.action, "quantity_required")
    if op.action in ("multiply", "divide") and op.factor is None:
        return _operation_error(op.action, "factor_required")
    assignee_id, assignee_error = await _assignee_id(session, op)
    if assignee_error is not None:
        return assignee_error

    kwargs: dict[str, Any] = {
        "store": op.store,
        "general": op.general,
        "list_id": op.list_id,
    }
    if op.category is not None and op.category.strip():
        category = op.category.strip()
    elif isinstance(item_ref, str):
        category = categories.infer_category(item_ref)
    else:
        category = "прочее"
    currency = op.currency or "RUB"
    result: service.MutationResult
    if op.action == "add":
        if not isinstance(item_ref, str):
            return _operation_error(op.action, "name_required")
        result = await service.add(
            session,
            user_id,
            item_ref,
            op.quantity,
            op.unit,
            category=category,
            planned_for=op.planned_for,
            assigned_to=assignee_id,
            price_amount=op.price,
            price_currency=currency,
            price_kind=op.price_kind,
            allow_incompatible_unit=op.allow_incompatible_unit,
            **kwargs,
        )
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "set":
        result = await service.set_quantity(
            session, user_id, item_ref, op.quantity or 0, op.unit, **kwargs
        )
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "increase":
        result = await service.increase(
            session, user_id, item_ref, op.quantity or 0, op.unit, **kwargs
        )
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "decrease":
        result = await service.decrease(
            session, user_id, item_ref, op.quantity or 0, op.unit, **kwargs
        )
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "multiply":
        result = await service.multiply(session, user_id, item_ref, op.factor or 0, **kwargs)
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "divide":
        result = await service.divide(session, user_id, item_ref, op.factor or 0, **kwargs)
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "remove":
        result = await service.remove(session, user_id, item_ref, **kwargs)
        return await _finish_mutation(session, result, op, "pending")
    if op.action == "mark_pending":
        result = await service.mark_pending(session, user_id, item_ref, **kwargs)
        return await _finish_mutation(session, result, op, "bought")

    result = await service.mark_bought(
        session,
        user_id,
        item_ref,
        op.quantity,
        op.unit,
        category=category,
        planned_for=op.planned_for,
        assigned_to=assignee_id,
        price_amount=op.price,
        price_currency=currency,
        price_kind=op.price_kind,
        **kwargs,
    )
    return await _finish_mutation(session, result, op, "pending")


PURCHASE_UPDATE_RUN_LIMIT = 3
PURCHASE_UPDATE_BATCH_LIMIT = 50

_FACTOR_PHRASE_RE = re.compile(
    r"(?<!\w)(?:в|во)\s+"
    r"(?P<value>\d+(?:[.,]\d+)?|полтора|полторы|два|две|три)\s+раза\s+"
    r"(?P<direction>больше|меньше)(?!\w)"
)
# Even an unsupported number word is still a factor phrase for fail-closed
# ambiguity detection; it just cannot supply a value.
_FACTOR_ANY_PHRASE_RE = re.compile(
    r"(?<!\w)(?:в|во)\s+[\w.,-]+\s+раза\s+(?:больше|меньше)(?!\w)"
)
_FACTOR_WORDS = {
    "полтора": 1.5,
    "полторы": 1.5,
    "два": 2.0,
    "две": 2.0,
    "три": 3.0,
}
_ASSIGNEE_PHRASE_RE = re.compile(
    r"^\s*(?:[^\n:]{1,80}:\s*)?"
    r"(?P<name>[A-ZА-ЯЁ][\w-]*)\s+"
    r"(?:должен|должна)\s+купить(?!\w)"
)
_ASSIGNEE_STOP_WORDS = {"я", "ты", "он", "она", "мы", "вы", "они", "кто"}
_CONTRASTING_RESPONSIBILITY_RE = re.compile(
    r"(?<!\w)а\s+[\w-]+\s+"
    r"(?:куплю|купим|купишь|купит|купят|должен|должна|должны)(?!\w)"
)
_STORE_PHRASE_RES = (
    (
        re.compile(r"(?<!\w)в\s+(?:пятерочке|pyaterochka)(?!\w)"),
        "Пятёрочка",
    ),
    (re.compile(r"(?<!\w)в\s+(?:спаре|спар|spar)(?!\w)"), "SPAR"),
    (re.compile(r"(?<!\w)в\s+(?:магните|магнит|magnit)(?!\w)"), "Магнит"),
    (re.compile(r"(?<!\w)в\s+(?:ленте|лента|lenta)(?!\w)"), "Лента"),
    (re.compile(r"(?<!\w)в\s+(?:ашане|ашан|ashan)(?!\w)"), "Ашан"),
)


def _normalized_trigger(trigger_text: str) -> str:
    return trigger_text.casefold().replace("ё", "е")


def _factor_phrases(trigger_text: str) -> dict[str, set[float]]:
    values: dict[str, set[float]] = {"больше": set(), "меньше": set()}
    for match in _FACTOR_PHRASE_RE.finditer(_normalized_trigger(trigger_text)):
        raw = match.group("value")
        factor = _FACTOR_WORDS.get(raw)
        if factor is None:
            factor = float(raw.replace(",", "."))
        if factor > 0:
            values[match.group("direction")].add(factor)
    return values


def _assignee_fallback(trigger_text: str) -> str | None:
    if _CONTRASTING_RESPONSIBILITY_RE.search(_normalized_trigger(trigger_text)):
        return None
    names: dict[str, str] = {}
    for match in _ASSIGNEE_PHRASE_RE.finditer(trigger_text):
        name = match.group("name")
        key = name.casefold().replace("ё", "е")
        if key not in _ASSIGNEE_STOP_WORDS:
            names.setdefault(key, name)
    return next(iter(names.values())) if len(names) == 1 else None


def _store_fallback(trigger_text: str) -> str | None:
    normalized = _normalized_trigger(trigger_text)
    stores = {
        canonical
        for pattern, canonical in _STORE_PHRASE_RES
        if pattern.search(normalized)
    }
    return next(iter(stores)) if len(stores) == 1 else None


def _operations_with_trigger_fallbacks(
    trigger_text: str, operations: list[PurchaseOperation]
) -> list[PurchaseOperation]:
    """Fill high-value omissions from obvious trigger phrases, conservatively.

    This is only a safety net for weak local models: the skill/model remains the
    NLU layer, explicit operation fields always win, and mixed/complex batches
    are left untouched.
    """
    if not trigger_text.strip() or not operations:
        return operations

    updates: list[dict[str, Any]] = [{} for _ in operations]
    factor_phrases = _factor_phrases(trigger_text)
    directions = {"multiply": "больше", "divide": "меньше"}
    factor_operations = [
        index for index, op in enumerate(operations) if op.action in directions
    ]
    missing_factor = [
        index for index in factor_operations if operations[index].factor is None
    ]
    phrase_count = len(_FACTOR_ANY_PHRASE_RE.findall(_normalized_trigger(trigger_text)))
    if len(factor_operations) == 1 and len(missing_factor) == 1 and phrase_count == 1:
        index = missing_factor[0]
        direction = directions[operations[index].action]
        candidates = factor_phrases[direction]
        if len(candidates) == 1:
            updates[index]["factor"] = next(iter(candidates))

    uniform_add_batch = all(op.action == "add" for op in operations)
    if uniform_add_batch:
        assignee = _assignee_fallback(trigger_text)
        assignee_omitted = all(
            op.assignee is None or not op.assignee.strip() for op in operations
        )
        if assignee is not None and assignee_omitted:
            for update in updates:
                update["assignee"] = assignee

        store = _store_fallback(trigger_text)
        store_omitted = all(
            op.store is None or not op.store.strip() for op in operations
        )
        # A general=true operation is an explicit non-store destination; mixed
        # destinations are too ambiguous for this fallback.
        if store is not None and store_omitted and not any(
            op.general for op in operations
        ):
            for update in updates:
                update["store"] = store

    return [
        op.model_copy(update=update) if update else op
        for op, update in zip(operations, updates, strict=True)
    ]


def _batch_key(operations: list[PurchaseOperation]) -> str:
    """Stable per-run key for exact replay detection (operation order included)."""
    return json.dumps(
        [op.model_dump(mode="json") for op in operations],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def purchase_update(
    ctx: RunContext[PurchaseToolDeps], operations: list[PurchaseOperation]
) -> dict[str, Any]:
    """Apply a batch of purchase-list mutations.

    Use exact `item_id` values returned by purchase_query/checklist answers for
    state changes; use `name` for natural additions/corrections. Exact `list_id`
    targets one editable list after ambiguity and overrides store/context/general
    without bypassing permissions. Without an explicit store, a non-general
    operation uses the user's current store
    context (or their general list when no context is active). Errors include
    list/item/user candidates suitable for ask_user_question.
    """
    if not operations:
        return {"ok": False, "error": "operations_required", "results": []}
    if len(operations) > PURCHASE_UPDATE_BATCH_LIMIT:
        return {
            "ok": False,
            "error": "too_many_operations",
            "operation_count": len(operations),
            "operation_limit": PURCHASE_UPDATE_BATCH_LIMIT,
            "instruction": (
                "Split the purchase update into batches of at most "
                f"{PURCHASE_UPDATE_BATCH_LIMIT} operations."
            ),
            "results": [],
        }
    operations = _operations_with_trigger_fallbacks(ctx.deps.trigger_text, operations)
    key = _batch_key(operations)
    async with ctx.deps.purchase_update_lock:
        ctx.deps.purchase_update_count += 1
        call_count = ctx.deps.purchase_update_count
        if call_count > PURCHASE_UPDATE_RUN_LIMIT:
            return {
                "ok": False,
                "error": "purchase_update_loop_detected",
                "detail": (
                    "purchase_update was called more than "
                    f"{PURCHASE_UPDATE_RUN_LIMIT} times in one agent run; "
                    "no further mutations were applied"
                ),
                "purchase_update_count": call_count,
                "purchase_update_limit": PURCHASE_UPDATE_RUN_LIMIT,
                "terminal": True,
                "instruction": (
                    "Do not call purchase_update again in this turn; "
                    "explain the problem or ask the user for clarification."
                ),
                "results": [],
            }
        cached = ctx.deps.purchase_update_results.get(key)
        if cached is not None:
            replay = copy.deepcopy(cached)
            replay["deduplicated"] = True
            replay["purchase_update_count"] = call_count
            return replay

        async with get_sessionmaker()() as session:
            if await session.get(User, ctx.deps.user_id) is None:
                result = {"ok": False, "error": "user not found", "results": []}
            else:
                results: list[PurchaseOperationResult] = []
                for op in operations:
                    try:
                        results.append(await _apply_operation(session, ctx.deps.user_id, op))
                    except (service.PurchaseError, ValueError) as e:
                        results.append(
                            _operation_error(op.action, str(e) or "invalid_operation")
                        )
                result = {
                    "ok": all(row.ok for row in results),
                    "results": [row.model_dump(mode="json") for row in results],
                }
        ctx.deps.purchase_update_results[key] = copy.deepcopy(result)
        return result


def _view_item(item: service.ItemView) -> dict[str, Any]:
    return {
        "id": item.id,
        "list_id": item.list_id,
        "name": item.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "category": item.category,
        "status": item.status,
        "planned_for": item.planned_for.isoformat(),
        "price": item.price_amount,
        "currency": item.price_currency,
        "price_kind": item.price_kind,
        "assigned_to": item.assigned_to,
        "assignee_name": item.assignee_name,
    }


def _view_list(plist: service.ListView) -> dict[str, Any]:
    return {
        "id": plist.id,
        "owner_id": plist.owner_id,
        "owner_name": plist.owner_name,
        "store_key": plist.store_key,
        "store_name": plist.store_name,
        "title": plist.title,
        "access": plist.access,
        "items": [_view_item(item) for item in plist.items],
    }


def _context_out(context: service.StoreContextView | None) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "store_key": context.store_key,
        "store_name": context.store_name,
        "updated_at": context.updated_at.isoformat(),
    }


async def _selected_query_store(
    session: AsyncSession,
    user_id: int,
    store: str | None,
    general: bool,
    all_stores: bool,
) -> str | None:
    """None = no store filter; "" = general list; otherwise canonical key."""
    if all_stores:
        return None
    if store is not None and store.strip():
        key, _name = service.normalize_store(store)
        return key
    if general:
        return ""
    context = await service.get_context(session, user_id)
    return context.store_key if context is not None else ""


async def purchase_query(
    ctx: RunContext[PurchaseToolDeps],
    store: str | None = None,
    general: bool = False,
    all_stores: bool = False,
    status: Literal["pending", "bought", "all"] = "pending",
    for_date: date | None = None,
    include_future: bool = False,
    assigned_only: bool = False,
) -> dict[str, Any]:
    """Query family-visible purchase lists.

    By default this reads the current-store list when a store context is
    active, otherwise the general/non-store list; `all_stores=true` disables
    that filtering. Items due today or overdue are included; future items are
    hidden unless `include_future` is true. `for_date` moves the due-date
    horizon (useful for «что купить завтра»). `assigned_only=true` keeps only
    items assigned to the caller.
    """
    async with get_sessionmaker()() as session:
        if await session.get(User, ctx.deps.user_id) is None:
            return {"error": "user not found"}
        try:
            store_key = await _selected_query_store(
                session, ctx.deps.user_id, store, general, all_stores
            )
            result = await service.query(
                session,
                ctx.deps.user_id,
                mode="assigned" if assigned_only else "family",
                status=None if status == "all" else status,
                store=store_key if store_key else None,
                include_future=include_future,
                today=for_date or service.family_today(),
            )
        except service.PurchaseError as e:
            return {"error": str(e)}
        if store_key == "":
            result.lists = [plist for plist in result.lists if not plist.store_key]
        markdown = render.render_markdown(result)
        spoken = render.render_spoken(result)
        lists = [_view_list(plist) for plist in result.lists]
        context = _context_out(result.context)
    ctx.deps.spoken_text = spoken
    return {
        "lists": lists,
        "markdown": markdown,
        "spoken_text": spoken,
        "store_context": context,
        "status": status,
        "for_date": (for_date or service.family_today()).isoformat(),
        "include_future": include_future,
        "assigned_only": assigned_only,
    }


async def purchase_store(
    ctx: RunContext[PurchaseToolDeps],
    action: Literal["set", "clear", "current"],
    store: str | None = None,
) -> dict[str, Any]:
    """Set, clear or inspect the user's current physical store context."""
    async with get_sessionmaker()() as session:
        if await session.get(User, ctx.deps.user_id) is None:
            return {"error": "user not found"}
        try:
            if action == "set":
                if store is None or not store.strip():
                    return {"error": "store_required"}
                context_row = await service.set_context(session, ctx.deps.user_id, store)
                return {
                    "ok": True,
                    "action": action,
                    "store_context": {
                        "store_key": context_row.store_key,
                        "store_name": context_row.store_name,
                        "updated_at": context_row.updated_at.isoformat(),
                    },
                }
            if action == "clear":
                existed = await service.get_context(session, ctx.deps.user_id) is not None
                await service.clear_context(session, ctx.deps.user_id)
                return {"ok": True, "action": action, "cleared": existed}
        except service.PurchaseError as e:
            return {"error": str(e)}
        row = await service.get_context(session, ctx.deps.user_id)
        return {
            "ok": True,
            "action": "current",
            "store_context": (
                {
                    "store_key": row.store_key,
                    "store_name": row.store_name,
                    "updated_at": row.updated_at.isoformat(),
                }
                if row is not None
                else None
            ),
        }


async def purchase_share(
    ctx: RunContext[PurchaseToolDeps],
    users: list[str],
    store: str | None = None,
    general: bool = False,
    mode: Literal["viewer", "editor"] = "editor",
    revoke: bool = False,
) -> dict[str, Any]:
    """Share/unshare the caller's own list with users by name.

    Explicit sharing defaults to editor access. Only the list owner can grant
    or revoke access; family users otherwise have read-only visibility, while
    guests have no implicit visibility at all.
    """
    if not users:
        return {"error": "users_required"}
    async with get_sessionmaker()() as session:
        owner = await session.get(User, ctx.deps.user_id)
        if owner is None:
            return {"error": "user not found"}
        try:
            target = await service.resolve_target(
                session,
                owner,
                store=store,
                general=general,
                create=True,
            )
        except service.PurchaseError as e:
            return {"error": str(e)}
        if target.error is not None or target.purchase_list is None:
            list_candidates = _list_candidates(target.candidates)
            error = target.error or "not_found"
            return {
                "error": error,
                "candidates": list_candidates,
                **_guidance_fields(error, list_candidates),
            }
        if target.access != "owner":
            return {"error": "denied", "detail": "only the owner may share a list"}
        plist = target.purchase_list
        results: list[dict[str, Any]] = []
        for name in users:
            user, candidates = await _resolve_user(session, name)
            if user is None:
                user_candidates = [
                    {"kind": "user", "name": candidate} for candidate in candidates
                ]
                error = "ambiguous_user" if candidates else "user_not_found"
                results.append(
                    {
                        "user": name,
                        "ok": False,
                        "error": error,
                        "candidates": user_candidates,
                        **_guidance_fields(error, user_candidates),
                    }
                )
                continue
            if user.id == owner.id:
                results.append(
                    {
                        "user": name,
                        "user_id": user.id,
                        "ok": False,
                        "error": "owner_has_implicit_access",
                    }
                )
                continue
            try:
                if revoke:
                    removed = await service.unshare_list(session, owner, plist.id, user.id)
                    results.append(
                        {
                            "user": user.display_name,
                            "user_id": user.id,
                            "ok": removed,
                            "revoked": removed,
                        }
                    )
                else:
                    member = await service.share_list(session, owner, plist.id, user.id, mode)
                    results.append(
                        {
                            "user": user.display_name,
                            "user_id": user.id,
                            "ok": True,
                            "mode": member.role,
                        }
                    )
            except service.PurchaseError as e:
                results.append(
                    {"user": name, "user_id": user.id, "ok": False, "error": str(e)}
                )
        return {
            "ok": all(row.get("ok") for row in results),
            "list": _list_out(plist),
            "revoke": revoke,
            "results": results,
        }
