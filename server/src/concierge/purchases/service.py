# ruff: noqa: RUF001, RUF002
"""Purchase-list domain service.

This module is deliberately independent of the agent runtime: later tools and
channel integrations call these functions, while permissions, deduplication,
dates and store context stay deterministic and testable here.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.core.config import get_settings
from concierge.core.db import utcnow
from concierge.purchases.models import (
    PurchaseContext,
    PurchaseItem,
    PurchaseList,
    PurchaseListMember,
)
from concierge.purchases.units import (
    QuantityInput,
    convert_quantity,
    normalize_unit,
    practical_number,
    quantity_decimal,
    unit_dimension,
    units_mergeable,
)

CONTEXT_TTL = timedelta(hours=4)

type AccessLevel = Literal[
    "owner", "editor", "viewer", "family", "assignee", "none"
]
type QueryMode = Literal["own", "assigned", "family"]
type ItemRef = int | str

_FULL_MUTATION_ACCESS = {"owner", "editor"}
_FULL_READ_ACCESS = {"owner", "editor", "viewer", "family"}


class PurchaseError(ValueError):
    """Invalid purchase-domain operation or permission violation."""


@dataclass(slots=True)
class ListRef:
    id: int
    owner_id: int
    owner_name: str
    store_key: str
    store_name: str | None
    access: str


@dataclass(slots=True)
class TargetResolution:
    purchase_list: PurchaseList | None
    access: AccessLevel = "none"
    error: str | None = None
    candidates: list[ListRef] = field(default_factory=list)


@dataclass(slots=True)
class MutationResult:
    ok: bool
    item: PurchaseItem | None = None
    purchase_list: PurchaseList | None = None
    error: str | None = None
    candidates: list[ListRef] = field(default_factory=list)
    item_candidates: list[tuple[PurchaseItem, PurchaseList]] = field(
        default_factory=list
    )


@dataclass(slots=True)
class PurchaseItemInput:
    name: str
    quantity: QuantityInput | None = None
    unit: str | None = None
    category: str = "прочее"
    planned_for: date | None = None
    assigned_to: int | None = None
    price_amount: QuantityInput | None = None
    price_currency: str = "RUB"
    price_kind: str | None = None
    allow_incompatible_unit: bool = False


@dataclass(slots=True)
class BatchMutationResult:
    ok: bool
    results: list[MutationResult] = field(default_factory=list)
    error: str | None = None
    candidates: list[ListRef] = field(default_factory=list)


@dataclass(slots=True)
class StoreContextView:
    store_key: str
    store_name: str
    updated_at: datetime


@dataclass(slots=True)
class ItemView:
    id: int
    list_id: int
    name: str
    quantity: int | float
    unit: str
    category: str
    status: str
    planned_for: date
    price_amount: int | float | None
    price_currency: str
    price_kind: str | None
    assigned_to: int | None
    assignee_name: str | None


@dataclass(slots=True)
class ListView:
    id: int
    owner_id: int
    owner_name: str
    store_key: str
    store_name: str | None
    title: str
    access: AccessLevel
    items: list[ItemView] = field(default_factory=list)


@dataclass(slots=True)
class QueryResult:
    lists: list[ListView]
    context: StoreContextView | None


@dataclass(slots=True)
class _ItemTarget:
    item: PurchaseItem | None
    purchase_list: PurchaseList | None
    access: AccessLevel = "none"
    error: str | None = None
    candidates: list[ListRef] = field(default_factory=list)
    item_candidates: list[tuple[PurchaseItem, PurchaseList]] = field(
        default_factory=list
    )


_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_PRODUCT_SYNONYMS = {
    "картошка": "картофель",
    "сгущенка": "сгущенное молоко",
}

_STORE_ALIASES = {
    "spar": ("spar", "SPAR"),
    "спар": ("spar", "SPAR"),
    "pyaterochka": ("pyaterochka", "Пятёрочка"),
    "пятерочка": ("pyaterochka", "Пятёрочка"),
    "magnit": ("magnit", "Магнит"),
    "магнит": ("magnit", "Магнит"),
    "lenta": ("lenta", "Лента"),
    "лента": ("lenta", "Лента"),
    "ashan": ("ashan", "Ашан"),
    "ашан": ("ashan", "Ашан"),
}


def _compact_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip())


def normalize_product_key(name: str) -> str:
    """Canonical product key: lowercase, ё→е, punctuation/whitespace normalized."""
    cleaned = _compact_text(
        _WORD_RE.sub(" ", name.casefold().replace("ё", "е"))
    )
    return _PRODUCT_SYNONYMS.get(cleaned, cleaned)


def normalize_store(store: str) -> tuple[str, str]:
    """Return `(store_key, display_name)` for a free-form store name."""
    display = _compact_text(store)
    if not display:
        raise PurchaseError("empty_store")
    simple = _compact_text(
        _WORD_RE.sub(" ", display.casefold().replace("ё", "е"))
    )
    if not simple:
        raise PurchaseError("empty_store")
    key, canonical = _STORE_ALIASES.get(simple, (simple, display))
    return key[:128], canonical[:128]


def family_today(now: datetime | None = None) -> date:
    """Today's date in the family timezone (the scheduler's timezone pattern)."""
    tz = ZoneInfo(get_settings().timezone)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(tz).date()


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _user_id(user: User | int) -> int:
    return user.id if isinstance(user, User) else user


async def _load_user(session: AsyncSession, user: User | int) -> User:
    row = await session.get(User, _user_id(user))
    if row is None:
        raise PurchaseError("unknown_user")
    return row


def _is_family(user: User) -> bool:
    return user.role.name != "guest"


def _title(plist: PurchaseList) -> str:
    if not plist.store_key:
        return "Покупки"
    return f"Купить в {plist.store_name or plist.store_key}"


async def _has_assignment(
    session: AsyncSession, list_id: int, user_id: int
) -> bool:
    row = await session.execute(
        select(PurchaseItem.id)
        .where(
            PurchaseItem.list_id == list_id,
            PurchaseItem.assigned_to == user_id,
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def access_level(
    session: AsyncSession, user: User | int, plist: PurchaseList
) -> AccessLevel:
    """Effective access of `user` to `plist`.

    Non-guest family users have implicit read-only access to lists owned by
    non-guest family users. Explicit membership is stronger for editors and is
    the only implicit-independent read path for guests.
    """
    actor = await _load_user(session, user)
    if plist.owner_id == actor.id:
        return "owner"
    member = await session.get(PurchaseListMember, (plist.id, actor.id))
    if member is not None and member.role in ("viewer", "editor"):
        return member.role  # type: ignore[return-value]
    owner = await session.get(User, plist.owner_id)
    if owner is not None and _is_family(actor) and _is_family(owner):
        return "family"
    if await _has_assignment(session, plist.id, actor.id):
        return "assignee"
    return "none"


def can_read(access: AccessLevel) -> bool:
    return access != "none"


def can_mutate_list(access: AccessLevel) -> bool:
    return access in _FULL_MUTATION_ACCESS


async def _list_ref(
    session: AsyncSession, plist: PurchaseList, access: AccessLevel
) -> ListRef:
    owner = await session.get(User, plist.owner_id)
    return ListRef(
        id=plist.id,
        owner_id=plist.owner_id,
        owner_name=owner.display_name if owner is not None else str(plist.owner_id),
        store_key=plist.store_key,
        store_name=plist.store_name,
        access=access,
    )


async def _create_context_with_retry(
    session: AsyncSession,
    user_id: int,
    store_key: str,
    store_name: str,
    timestamp: datetime,
) -> PurchaseContext:
    """Insert first store context once despite concurrent first-write races."""
    row = PurchaseContext(
        user_id=user_id,
        store_key=store_key,
        store_name=store_name,
        updated_at=timestamp,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        if row in session:
            session.expunge(row)
        existing = await session.get(PurchaseContext, user_id)
        if existing is not None:
            return existing
        raise
    return row


async def set_context(
    session: AsyncSession,
    user: User | int,
    store: str,
    now: datetime | None = None,
) -> PurchaseContext:
    key, name = normalize_store(store)
    timestamp = _utc_naive(now or utcnow())
    row = await session.get(PurchaseContext, _user_id(user))
    if row is None:
        row = await _create_context_with_retry(
            session, _user_id(user), key, name, timestamp
        )
    row.store_key = key
    row.store_name = name
    row.updated_at = timestamp
    await session.commit()
    return row


async def get_context(
    session: AsyncSession,
    user: User | int,
    now: datetime | None = None,
) -> PurchaseContext | None:
    row = await session.get(PurchaseContext, _user_id(user))
    if row is None:
        return None
    timestamp = _utc_naive(now or utcnow())
    if timestamp - row.updated_at >= CONTEXT_TTL:
        return None
    return row


async def clear_context(session: AsyncSession, user: User | int) -> None:
    row = await session.get(PurchaseContext, _user_id(user))
    if row is not None:
        await session.delete(row)
        await session.commit()


async def _selected_store(
    session: AsyncSession,
    user: User | int,
    store: str | None,
    general: bool,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    if store is not None and store.strip():
        return normalize_store(store)
    if general:
        return "", None
    context = await get_context(session, user, now)
    if context is not None:
        return context.store_key, context.store_name
    return "", None


async def _owned_list(
    session: AsyncSession, owner_id: int, store_key: str
) -> PurchaseList | None:
    return (
        await session.execute(
            select(PurchaseList).where(
                PurchaseList.owner_id == owner_id,
                PurchaseList.store_key == store_key,
            )
        )
    ).scalar_one_or_none()


async def _create_owned_list_with_retry(
    session: AsyncSession, owner_id: int, store_key: str, display: str | None
) -> PurchaseList:
    """Create `(owner, store)` once despite concurrent first-use races.

    The unique constraint is the authority. A nested rollback contains the
    expected duplicate-key failure without discarding caller work; the retry
    then converges on the row committed by the concurrent creator.
    """
    plist = PurchaseList(
        owner_id=owner_id,
        store_key=store_key,
        store_name=display if store_key else None,
    )
    try:
        async with session.begin_nested():
            session.add(plist)
            await session.flush()
    except IntegrityError:
        # Keep the losing pending insert out of the retry query's autoflush.
        # Nested rollback may already have removed it from the session.
        if plist in session:
            session.expunge(plist)
        existing = await _owned_list(session, owner_id, store_key)
        if existing is not None:
            return existing
        raise
    return plist


async def _resolve_target(
    session: AsyncSession,
    user: User | int,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
    create: bool = True,
    own_only: bool = False,
    now: datetime | None = None,
) -> TargetResolution:
    """Resolve a mutation target without treating family-readonly lists as editable."""
    actor_id = _user_id(user)
    if list_id is not None:
        plist = await session.get(PurchaseList, list_id)
        if plist is None:
            return TargetResolution(None, error="not_found")
        access = await access_level(session, actor_id, plist)
        if not can_mutate_list(access):
            return TargetResolution(plist, access, error="denied")
        return TargetResolution(plist, access)

    key, display = await _selected_store(session, actor_id, store, general, now)
    own = await _owned_list(session, actor_id, key)
    if own is not None:
        return TargetResolution(own, "owner")

    if not own_only and not general:
        shared = (
            (
                await session.execute(
                    select(PurchaseList)
                    .join(
                        PurchaseListMember,
                        PurchaseListMember.list_id == PurchaseList.id,
                    )
                    .where(
                        PurchaseListMember.user_id == actor_id,
                        PurchaseListMember.role == "editor",
                        PurchaseList.store_key == key,
                        PurchaseList.owner_id != actor_id,
                    )
                    .order_by(PurchaseList.id)
                )
            )
            .scalars()
            .all()
        )
        if len(shared) == 1:
            return TargetResolution(shared[0], "editor")
        if len(shared) > 1:
            candidates = [await _list_ref(session, row, "editor") for row in shared]
            return TargetResolution(None, error="ambiguous", candidates=candidates)

    if not create:
        return TargetResolution(None, error="not_found")
    plist = await _create_owned_list_with_retry(session, actor_id, key, display)
    return TargetResolution(plist, "owner")


async def resolve_target(
    session: AsyncSession,
    user: User | int,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
    create: bool = True,
    own_only: bool = False,
) -> TargetResolution:
    """Public target-resolution helper for later tools; commits a newly created row."""
    result = await _resolve_target(
        session,
        user,
        store=store,
        general=general,
        list_id=list_id,
        create=create,
        own_only=own_only,
    )
    await session.commit()
    return result


async def _create_list_member_with_retry(
    session: AsyncSession,
    list_id: int,
    target_user_id: int,
    role: str,
    added_by: int,
) -> PurchaseListMember:
    """Insert a share once despite concurrent duplicate-share races."""
    member = PurchaseListMember(
        list_id=list_id,
        user_id=target_user_id,
        role=role,
        added_by=added_by,
    )
    try:
        async with session.begin_nested():
            session.add(member)
            await session.flush()
    except IntegrityError:
        if member in session:
            session.expunge(member)
        existing = await session.get(PurchaseListMember, (list_id, target_user_id))
        if existing is not None:
            return existing
        raise
    return member


async def share_list(
    session: AsyncSession,
    owner: User | int,
    list_id: int,
    target_user_id: int,
    role: str = "editor",
) -> PurchaseListMember:
    """Grant explicit access; sharing defaults to editor by product decision."""
    if role not in ("viewer", "editor"):
        raise PurchaseError("bad_member_role")
    plist = await session.get(PurchaseList, list_id)
    if plist is None:
        raise PurchaseError("not_found")
    access = await access_level(session, owner, plist)
    if access != "owner":
        raise PurchaseError("denied")
    if target_user_id == plist.owner_id:
        raise PurchaseError("owner_has_implicit_access")
    target = await session.get(User, target_user_id)
    if target is None:
        raise PurchaseError("unknown_user")
    member = await session.get(PurchaseListMember, (list_id, target_user_id))
    if member is None:
        member = await _create_list_member_with_retry(
            session, list_id, target_user_id, role, _user_id(owner)
        )
    member.role = role
    member.added_by = _user_id(owner)
    await session.commit()
    return member


async def unshare_list(
    session: AsyncSession, owner: User | int, list_id: int, target_user_id: int
) -> bool:
    plist = await session.get(PurchaseList, list_id)
    if plist is None:
        raise PurchaseError("not_found")
    access = await access_level(session, owner, plist)
    if access != "owner":
        raise PurchaseError("denied")
    member = await session.get(PurchaseListMember, (list_id, target_user_id))
    if member is None:
        return False
    await session.delete(member)
    await session.commit()
    return True


def _failure(error: str, candidates: list[ListRef] | None = None) -> MutationResult:
    return MutationResult(False, error=error, candidates=candidates or [])


# Numeric(12,3) leaves nine integer digits for quantities; Numeric(12,2)
# leaves ten for prices. Reject overflow-shaped values before they reach the DB.
_MAX_QUANTITY = Decimal("999999999.999")
_MAX_PRICE = Decimal("9999999999.99")
_QUANTITY_SCALE = Decimal("0.001")
_PRICE_SCALE = Decimal("0.01")


def _stored_quantity_valid(value: Decimal) -> bool:
    return value > 0 and value <= _MAX_QUANTITY


def _schema_quantity(value: Decimal) -> Decimal:
    """Round to the persisted Numeric(12,3) scale before validation/output."""
    try:
        out = value.quantize(_QUANTITY_SCALE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("quantity cannot fit Numeric(12,3)") from None
    if not _stored_quantity_valid(out):
        raise ValueError("quantity must be positive and within Numeric(12,3)")
    return out


def _quantity(value: QuantityInput | None) -> Decimal:
    return _schema_quantity(quantity_decimal(value))


def _factor_value(value: QuantityInput) -> Decimal:
    """Positive multiplication factor; unlike quantities it is not stored."""
    out = quantity_decimal(value)
    if out <= 0 or out > _MAX_QUANTITY:
        raise ValueError("factor must be positive and within Numeric(12,3)")
    return out


def _unit_and_dimension(unit: str | None) -> tuple[str, str]:
    """Normalized unit plus dimension, checked against their String lengths."""
    unit_name = normalize_unit(unit)
    dimension = unit_dimension(unit_name)
    if len(unit_name) > 32 or len(dimension) > 48:
        raise ValueError("invalid_unit")
    return unit_name, dimension


def _money(value: QuantityInput | None) -> Decimal | None:
    if value is None:
        return None
    raw = quantity_decimal(value)
    try:
        out = raw.quantize(_PRICE_SCALE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError("price cannot fit Numeric(12,2)") from None
    if out < 0 or out > _MAX_PRICE:
        raise ValueError("price must be non-negative and within Numeric(12,2)")
    return out


def _valid_price_kind(kind: str | None) -> bool:
    return kind is None or kind in ("unit", "total")


async def _lock_list(session: AsyncSession, list_id: int) -> None:
    # PostgreSQL serializes concurrent add/dedupe on the list row; sqlite ignores
    # FOR UPDATE but keeps the same transaction shape for tests.
    await session.execute(
        select(PurchaseList.id).where(PurchaseList.id == list_id).with_for_update()
    )


def _apply_price(
    item: PurchaseItem,
    price_amount: QuantityInput | None,
    price_currency: str,
    price_kind: str | None,
) -> None:
    amount = _money(price_amount)
    if amount is not None:
        item.price_amount = amount
        item.price_currency = price_currency.strip().upper()[:8] or "RUB"
        item.price_kind = price_kind


async def add(
    session: AsyncSession,
    user: User | int,
    name: str,
    quantity: QuantityInput | None = None,
    unit: str | None = None,
    *,
    category: str = "прочее",
    planned_for: date | None = None,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
    assigned_to: int | None = None,
    price_amount: QuantityInput | None = None,
    price_currency: str = "RUB",
    price_kind: str | None = None,
    allow_incompatible_unit: bool = False,
    _commit: bool = True,
) -> MutationResult:
    """Add an item or merge it with the matching pending row."""
    display_name = _compact_text(name)
    canonical = normalize_product_key(display_name) if display_name else ""
    if not display_name or not canonical:
        return _failure("invalid_name")
    if not _valid_price_kind(price_kind):
        return _failure("invalid_price_kind")
    try:
        qty = _quantity(quantity)
        _money(price_amount)
    except ValueError:
        return _failure("invalid_quantity")
    try:
        unit_name, dimension = _unit_and_dimension(unit)
    except ValueError:
        return _failure("invalid_unit")
    if assigned_to is not None and await session.get(User, assigned_to) is None:
        return _failure("unknown_user")

    target = await _resolve_target(
        session,
        user,
        store=store,
        general=general,
        list_id=list_id,
        create=True,
    )
    if target.error is not None:
        return _failure(target.error, target.candidates)
    plist = target.purchase_list
    if plist is None or not can_mutate_list(target.access):
        return _failure("denied")

    planned = planned_for or family_today()
    now = utcnow()
    await _lock_list(session, plist.id)

    stmt = select(PurchaseItem).where(
        PurchaseItem.list_id == plist.id,
        PurchaseItem.canonical_key == canonical,
        PurchaseItem.planned_for == planned,
        PurchaseItem.status == "pending",
    )
    if assigned_to is None:
        stmt = stmt.where(PurchaseItem.assigned_to.is_(None))
    else:
        stmt = stmt.where(PurchaseItem.assigned_to == assigned_to)

    unit_omitted = unit is None or not unit.strip()
    existing_rows = list((await session.execute(stmt.order_by(PurchaseItem.id))).scalars())
    if quantity is None and unit_omitted and existing_rows:
        # «Закончились яблоки» carries no measurable amount. One existing row is
        # a duplicate reminder; several packaging/dimension variants cannot be
        # chosen deterministically and need explicit clarification instead.
        if len(existing_rows) > 1:
            return MutationResult(
                False,
                purchase_list=plist,
                error="ambiguous_item",
                item_candidates=[(row, plist) for row in existing_rows],
            )
        existing_row = existing_rows[0]
        if price_amount is not None:
            # A bare duplicate reminder can still carry explicit price metadata
            # («надо купить молоко по 80 рублей»). Merge that metadata without
            # touching quantity, unit, status, or row identity.
            _apply_price(existing_row, price_amount, price_currency, price_kind)
            existing_row.updated_at = now
            plist.updated_at = now
        if _commit:
            await session.commit()
        return MutationResult(True, item=existing_row, purchase_list=plist)

    same_dimension = [
        row for row in existing_rows if row.unit_dimension == dimension
    ]
    existing = next(
        (row for row in same_dimension if units_mergeable(row.unit, unit_name)),
        None,
    )
    if existing is not None:
        converted = convert_quantity(qty, unit_name, existing.unit)
        if converted is None:
            return _failure("incompatible_units")
        try:
            merged_quantity = _schema_quantity(existing.quantity + converted)
        except ValueError:
            return _failure("invalid_quantity")
        existing.quantity = merged_quantity
        existing.updated_at = now
        _apply_price(existing, price_amount, price_currency, price_kind)
        plist.updated_at = now
        if _commit:
            await session.commit()
        return MutationResult(True, item=existing, purchase_list=plist)

    if existing_rows and not same_dimension and not allow_incompatible_unit:
        # The product is already pending in a different unit dimension. Do not
        # paper over an explicit-but-incompatible amount by creating a shadow
        # duplicate row; the caller must clarify, edit the existing item, or
        # explicitly opt into separate-row creation after clarification.
        return MutationResult(
            False,
            item=existing_rows[0],
            purchase_list=plist,
            error="unit_mismatch",
        )

    item = PurchaseItem(
        list_id=plist.id,
        name=display_name[:256],
        canonical_key=canonical[:256],
        category=_compact_text(category)[:64] or "прочее",
        quantity=qty,
        unit=unit_name,
        unit_dimension=dimension,
        status="pending",
        planned_for=planned,
        price_amount=_money(price_amount),
        price_currency=price_currency.strip().upper()[:8] or "RUB",
        price_kind=price_kind,
        assigned_to=assigned_to,
        assigned_by=_user_id(user) if assigned_to is not None else None,
        added_by=_user_id(user),
    )
    session.add(item)
    plist.updated_at = now
    await session.flush()
    if _commit:
        await session.commit()
    return MutationResult(True, item=item, purchase_list=plist)


async def add_many(
    session: AsyncSession,
    user: User | int,
    items: Sequence[PurchaseItemInput],
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> BatchMutationResult:
    """Add a batch atomically; any structural failure rolls the whole batch back."""
    if not items:
        return BatchMutationResult(True)
    results: list[MutationResult] = []
    for spec in items:
        result = await add(
            session,
            user,
            spec.name,
            spec.quantity,
            spec.unit,
            category=spec.category,
            planned_for=spec.planned_for,
            store=store,
            general=general,
            list_id=list_id,
            assigned_to=spec.assigned_to,
            price_amount=spec.price_amount,
            price_currency=spec.price_currency,
            price_kind=spec.price_kind,
            allow_incompatible_unit=spec.allow_incompatible_unit,
            _commit=False,
        )
        if not result.ok:
            await session.rollback()
            return BatchMutationResult(
                False,
                error=result.error,
                candidates=result.candidates,
            )
        results.append(result)
    await session.commit()
    return BatchMutationResult(True, results=results)


async def _assigned_item_fallback(
    session: AsyncSession,
    user: User | int,
    name: str,
    status: str | None,
    store: str | None,
    general: bool,
) -> _ItemTarget:
    """Locate an assigned item when the list itself is not an editable target."""
    key, _display = await _selected_store(session, user, store, general)
    stmt = (
        select(PurchaseItem, PurchaseList)
        .join(PurchaseList, PurchaseItem.list_id == PurchaseList.id)
        .where(
            PurchaseItem.canonical_key == normalize_product_key(name),
            PurchaseItem.assigned_to == _user_id(user),
            PurchaseList.store_key == key,
        )
        .order_by(PurchaseItem.id)
    )
    if status is not None:
        stmt = stmt.where(PurchaseItem.status == status)
    rows = list((await session.execute(stmt)).all())
    if len(rows) != 1:
        return _ItemTarget(
            None,
            None,
            error="ambiguous_item" if rows else "not_found",
            item_candidates=[(item, plist) for item, plist in rows],
        )
    item, plist = rows[0]
    return _ItemTarget(item, plist, "assignee")


async def _lock_item_target(
    session: AsyncSession, found: _ItemTarget, status: str | None
) -> _ItemTarget:
    """Serialize a mutation on its final list and re-read the item under lock."""
    if found.error is not None or found.item is None or found.purchase_list is None:
        return found
    plist = found.purchase_list
    await _lock_list(session, plist.id)
    row = (
        await session.execute(
            select(PurchaseItem)
            .where(PurchaseItem.id == found.item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or (status is not None and row.status != status):
        return _ItemTarget(None, plist, found.access, error="not_found")
    return _ItemTarget(row, plist, found.access)


async def _find_mutation_item(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    *,
    status: str | None,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> _ItemTarget:
    if isinstance(item_ref, int):
        item = await session.get(PurchaseItem, item_ref)
        if item is None or (status is not None and item.status != status):
            return _ItemTarget(None, None, error="not_found")
        plist = await session.get(PurchaseList, item.list_id)
        if plist is None:
            return _ItemTarget(None, None, error="not_found")
        access = await access_level(session, user, plist)
        if access == "none":
            return _ItemTarget(item, plist, access, error="denied")
        if not can_mutate_list(access) and item.assigned_to != _user_id(user):
            return _ItemTarget(item, plist, access, error="denied")
        return await _lock_item_target(session, _ItemTarget(item, plist, access), status)

    target = await _resolve_target(
        session,
        user,
        store=store,
        general=general,
        list_id=list_id,
        create=False,
    )
    if target.error == "ambiguous":
        return _ItemTarget(None, None, error="ambiguous", candidates=target.candidates)
    if list_id is not None and target.error is not None:
        return _ItemTarget(None, None, error=target.error)
    if target.purchase_list is None:
        assigned = await _assigned_item_fallback(
            session, user, item_ref, status, store, general
        )
        return await _lock_item_target(session, assigned, status)
    plist = target.purchase_list
    stmt = select(PurchaseItem).where(
        PurchaseItem.list_id == plist.id,
        PurchaseItem.canonical_key == normalize_product_key(item_ref),
    )
    if status is not None:
        stmt = stmt.where(PurchaseItem.status == status)
    rows = list((await session.execute(stmt.order_by(PurchaseItem.id))).scalars())
    if not rows:
        if list_id is not None:
            return _ItemTarget(None, plist, target.access, error="not_found")
        assigned = await _assigned_item_fallback(
            session, user, item_ref, status, store, general
        )
        if assigned.item is not None or assigned.error == "ambiguous_item":
            return await _lock_item_target(session, assigned, status)
        return _ItemTarget(None, plist, target.access, error="not_found")
    if len(rows) > 1:
        return _ItemTarget(
            None,
            plist,
            target.access,
            error="ambiguous_item",
            item_candidates=[(item, plist) for item in rows],
        )
    item = rows[0]
    if not can_mutate_list(target.access) and item.assigned_to != _user_id(user):
        return _ItemTarget(item, plist, target.access, error="denied")
    return await _lock_item_target(
        session, _ItemTarget(item, plist, target.access), status
    )


def _mutation_failure(found: _ItemTarget) -> MutationResult:
    if found.error == "denied":
        # Denied exact-id probes must not echo item/list contents back through
        # the service or tool layer, even when the row exists.
        return MutationResult(False, error="denied")
    return MutationResult(
        False,
        item=found.item,
        purchase_list=found.purchase_list,
        error=found.error,
        candidates=found.candidates,
        item_candidates=found.item_candidates,
    )


async def set_quantity(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    quantity: QuantityInput,
    unit: str | None = None,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    """Exact correction («не два, а один») for a pending item."""
    try:
        qty = _quantity(quantity)
    except ValueError:
        return _failure("invalid_quantity")
    unit_name: str | None = None
    dimension: str | None = None
    if unit is not None:
        try:
            unit_name, dimension = _unit_and_dimension(unit)
        except ValueError:
            return _failure("invalid_unit")
    found = await _find_mutation_item(
        session, user, item_ref, status="pending", store=store, general=general, list_id=list_id
    )
    if found.error is not None or found.item is None:
        return _mutation_failure(found)
    item = found.item
    item.quantity = qty
    if unit is not None:
        item.unit = unit_name or "шт"
        item.unit_dimension = dimension or "count"
    item.updated_at = utcnow()
    await session.commit()
    return MutationResult(True, item=item, purchase_list=found.purchase_list)


async def _delta(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    amount: QuantityInput,
    sign: Decimal,
    unit: str | None,
    store: str | None,
    general: bool,
    list_id: int | None,
) -> MutationResult:
    try:
        qty = _quantity(amount)
    except ValueError:
        return _failure("invalid_quantity")
    found = await _find_mutation_item(
        session, user, item_ref, status="pending", store=store, general=general, list_id=list_id
    )
    if found.error is not None or found.item is None:
        return _mutation_failure(found)
    item = found.item
    source_unit = normalize_unit(unit) if unit is not None else item.unit
    converted = convert_quantity(qty, source_unit, item.unit)
    if converted is None:
        return MutationResult(
            False, item=item, purchase_list=found.purchase_list, error="incompatible_units"
        )
    try:
        new_quantity = _schema_quantity(item.quantity + sign * converted)
    except ValueError:
        return MutationResult(
            False, item=item, purchase_list=found.purchase_list, error="invalid_quantity"
        )
    item.quantity = new_quantity
    item.updated_at = utcnow()
    await session.commit()
    return MutationResult(True, item=item, purchase_list=found.purchase_list)


async def increase(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    amount: QuantityInput,
    unit: str | None = None,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    return await _delta(
        session, user, item_ref, amount, Decimal("1"), unit, store, general, list_id
    )


async def decrease(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    amount: QuantityInput,
    unit: str | None = None,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    return await _delta(
        session, user, item_ref, amount, Decimal("-1"), unit, store, general, list_id
    )


async def _factor(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    factor: QuantityInput,
    multiply: bool,
    store: str | None,
    general: bool,
    list_id: int | None,
) -> MutationResult:
    try:
        value = _factor_value(factor)
    except ValueError:
        return _failure("invalid_quantity")
    found = await _find_mutation_item(
        session, user, item_ref, status="pending", store=store, general=general, list_id=list_id
    )
    if found.error is not None or found.item is None:
        return _mutation_failure(found)
    item = found.item
    raw_quantity = item.quantity * value if multiply else item.quantity / value
    try:
        new_quantity = _schema_quantity(raw_quantity)
    except ValueError:
        return MutationResult(
            False, item=item, purchase_list=found.purchase_list, error="invalid_quantity"
        )
    item.quantity = new_quantity
    item.updated_at = utcnow()
    await session.commit()
    return MutationResult(True, item=item, purchase_list=found.purchase_list)


async def multiply(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    factor: QuantityInput,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    return await _factor(session, user, item_ref, factor, True, store, general, list_id)


async def divide(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    factor: QuantityInput,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    return await _factor(session, user, item_ref, factor, False, store, general, list_id)


async def remove(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    """Remove a pending item. Assignee-only access is not enough for deletion."""
    found = await _find_mutation_item(
        session, user, item_ref, status="pending", store=store, general=general, list_id=list_id
    )
    if found.error is not None or found.item is None:
        return _mutation_failure(found)
    if not can_mutate_list(found.access):
        return MutationResult(False, error="denied")
    item = found.item
    plist = found.purchase_list
    await session.delete(item)
    await session.commit()
    return MutationResult(True, item=item, purchase_list=plist)


async def _items_in_list_for_state(
    session: AsyncSession,
    plist: PurchaseList,
    canonical: str,
    status: str,
) -> list[PurchaseItem]:
    """Rows used by the authoritative locked mark_bought recheck."""
    stmt = select(PurchaseItem).where(
        PurchaseItem.list_id == plist.id,
        PurchaseItem.canonical_key == canonical,
        PurchaseItem.status == status,
    )
    return list(
        (
            await session.execute(
                stmt.order_by(PurchaseItem.id).execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def mark_bought(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    quantity: QuantityInput | None = None,
    unit: str | None = None,
    *,
    category: str = "прочее",
    planned_for: date | None = None,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
    assigned_to: int | None = None,
    price_amount: QuantityInput | None = None,
    price_currency: str = "RUB",
    price_kind: str | None = None,
) -> MutationResult:
    """Mark pending as bought, or create an already-bought item when unknown."""
    if not _valid_price_kind(price_kind):
        return _failure("invalid_price_kind")
    try:
        qty = _quantity(quantity)
        _money(price_amount)
    except ValueError:
        return _failure("invalid_quantity")
    try:
        unit_name, dimension = _unit_and_dimension(unit)
    except ValueError:
        return _failure("invalid_unit")
    display_name = _compact_text(item_ref) if isinstance(item_ref, str) else ""
    canonical = normalize_product_key(display_name) if display_name else ""
    if isinstance(item_ref, str) and (not display_name or not canonical):
        return _failure("invalid_name")
    if assigned_to is not None and await session.get(User, assigned_to) is None:
        return _failure("unknown_user")

    found = await _find_mutation_item(
        session, user, item_ref, status="pending", store=store, general=general, list_id=list_id
    )
    if found.error is None and found.item is not None:
        item = found.item
        item.status = "bought"
        item.bought_by = _user_id(user)
        item.bought_at = utcnow()
        item.updated_at = utcnow()
        _apply_price(item, price_amount, price_currency, price_kind)
        await session.commit()
        return MutationResult(True, item=item, purchase_list=found.purchase_list)
    if found.error not in ("not_found",):
        return _mutation_failure(found)

    already = await _find_mutation_item(
        session, user, item_ref, status="bought", store=store, general=general, list_id=list_id
    )
    if already.error is None and already.item is not None:
        if isinstance(item_ref, int):
            # Exact-id double-submit is a true no-op: state and metadata are
            # already the requested ones, and access was checked above.
            return MutationResult(
                True, item=already.item, purchase_list=already.purchase_list
            )
        _apply_price(already.item, price_amount, price_currency, price_kind)
        already.item.updated_at = utcnow()
        await session.commit()
        return MutationResult(True, item=already.item, purchase_list=already.purchase_list)
    if isinstance(item_ref, int):
        if already.error not in (None, "not_found"):
            return _mutation_failure(already)
        return _failure("not_found")

    # Unknown purchases always land in the caller's OWN explicit/context-store
    # list, or in the caller's own general list — never in a shared fallback.
    target = await _resolve_target(
        session,
        user,
        store=store,
        general=general,
        list_id=list_id,
        create=True,
        own_only=True,
    )
    if target.error is not None or target.purchase_list is None:
        return _failure(target.error or "not_found", target.candidates)
    plist = target.purchase_list

    # The pending/bought lookups above are only fast paths. Before creating an
    # unknown bought row, serialize on the final target list and repeat the
    # state checks under that lock; a concurrent identical call then becomes
    # the normal transition/idempotent path instead of a duplicate insert.
    await _lock_list(session, plist.id)
    for existing_status in ("pending", "bought"):
        existing_rows = await _items_in_list_for_state(
            session, plist, canonical, existing_status
        )
        if len(existing_rows) > 1:
            return MutationResult(
                False,
                purchase_list=plist,
                error="ambiguous_item",
                item_candidates=[(row, plist) for row in existing_rows],
            )
        if not existing_rows:
            continue
        existing = existing_rows[0]
        now = utcnow()
        if existing_status == "pending":
            existing.status = "bought"
            existing.bought_by = _user_id(user)
            existing.bought_at = now
        _apply_price(existing, price_amount, price_currency, price_kind)
        existing.updated_at = now
        await session.commit()
        return MutationResult(True, item=existing, purchase_list=plist)

    now = utcnow()
    item = PurchaseItem(
        list_id=plist.id,
        name=display_name[:256],
        canonical_key=canonical[:256],
        category=_compact_text(category)[:64] or "прочее",
        quantity=qty,
        unit=unit_name,
        unit_dimension=dimension,
        status="bought",
        planned_for=planned_for or family_today(),
        price_amount=_money(price_amount),
        price_currency=price_currency.strip().upper()[:8] or "RUB",
        price_kind=price_kind,
        assigned_to=assigned_to,
        assigned_by=_user_id(user) if assigned_to is not None else None,
        added_by=_user_id(user),
        bought_by=_user_id(user),
        bought_at=now,
    )
    session.add(item)
    plist.updated_at = now
    await session.flush()
    await session.commit()
    return MutationResult(True, item=item, purchase_list=plist)


async def mark_pending(
    session: AsyncSession,
    user: User | int,
    item_ref: ItemRef,
    *,
    store: str | None = None,
    general: bool = False,
    list_id: int | None = None,
) -> MutationResult:
    """Restore a bought item to pending."""
    found = await _find_mutation_item(
        session, user, item_ref, status="bought", store=store, general=general, list_id=list_id
    )
    if found.error is not None or found.item is None:
        if isinstance(item_ref, int) and found.error == "not_found":
            already = await _find_mutation_item(
                session,
                user,
                item_ref,
                status="pending",
                store=store,
                general=general,
                list_id=list_id,
            )
            if already.error is None and already.item is not None:
                # Exact-id double-submit is a no-op once access has been checked.
                return MutationResult(
                    True, item=already.item, purchase_list=already.purchase_list
                )
            if already.error not in (None, "not_found"):
                return _mutation_failure(already)
        return _mutation_failure(found)
    item = found.item
    item.status = "pending"
    item.bought_by = None
    item.bought_at = None
    item.updated_at = utcnow()
    await session.commit()
    return MutationResult(True, item=item, purchase_list=found.purchase_list)


async def assign_item(
    session: AsyncSession,
    user: User | int,
    item_id: int,
    assignee_id: int | None,
) -> MutationResult:
    """Assign/unassign an item; only owner/editor may change assignment."""
    found = await _find_mutation_item(session, user, item_id, status=None)
    if found.error is not None or found.item is None:
        return _mutation_failure(found)
    if not can_mutate_list(found.access):
        return MutationResult(False, error="denied")
    if assignee_id is not None and await session.get(User, assignee_id) is None:
        return MutationResult(
            False, item=found.item, purchase_list=found.purchase_list, error="unknown_user"
        )
    found.item.assigned_to = assignee_id
    found.item.assigned_by = _user_id(user) if assignee_id is not None else None
    found.item.updated_at = utcnow()
    await session.commit()
    return MutationResult(True, item=found.item, purchase_list=found.purchase_list)


def _context_view(row: PurchaseContext | None) -> StoreContextView | None:
    if row is None:
        return None
    return StoreContextView(row.store_key, row.store_name, row.updated_at)


async def query(
    session: AsyncSession,
    user: User | int,
    *,
    mode: QueryMode = "family",
    status: str | None = None,
    store: str | None = None,
    include_future: bool = False,
    today: date | None = None,
) -> QueryResult:
    """Structured purchase query used by renderers and later agent tools."""
    if mode not in ("own", "assigned", "family"):
        raise PurchaseError("bad_mode")
    if status is not None and status not in ("pending", "bought"):
        raise PurchaseError("bad_status")
    actor = await _load_user(session, user)
    context = await get_context(session, actor)
    store_key: str | None = None
    if store is not None and store.strip():
        store_key, _name = normalize_store(store)

    list_stmt = select(PurchaseList).order_by(PurchaseList.owner_id, PurchaseList.id)
    if store_key is not None:
        list_stmt = list_stmt.where(PurchaseList.store_key == store_key)
    all_lists = list((await session.execute(list_stmt)).scalars())
    owner_ids = {row.owner_id for row in all_lists}
    owners: dict[int, User] = {}
    if owner_ids:
        owner_rows = (await session.execute(select(User).where(User.id.in_(owner_ids)))).scalars()
        owners = {row.id: row for row in owner_rows}

    visible: list[tuple[PurchaseList, AccessLevel]] = []
    for plist in all_lists:
        access = await access_level(session, actor, plist)
        if mode == "own" and access != "owner":
            continue
        if access == "none":
            continue
        visible.append((plist, access))

    list_ids = [row.id for row, _access in visible]
    items_by_list: dict[int, list[PurchaseItem]] = {row_id: [] for row_id in list_ids}
    if list_ids:
        item_stmt = select(PurchaseItem).where(PurchaseItem.list_id.in_(list_ids))
        if status is not None:
            item_stmt = item_stmt.where(PurchaseItem.status == status)
        if not include_future:
            item_stmt = item_stmt.where(PurchaseItem.planned_for <= (today or family_today()))
        item_stmt = item_stmt.order_by(
            PurchaseItem.list_id,
            PurchaseItem.category,
            PurchaseItem.canonical_key,
            PurchaseItem.id,
        )
        for item in (await session.execute(item_stmt)).scalars():
            items_by_list.setdefault(item.list_id, []).append(item)

    assignee_ids = {
        item.assigned_to
        for rows in items_by_list.values()
        for item in rows
        if item.assigned_to is not None
    }
    assignees: dict[int, User] = {}
    if assignee_ids:
        rows = (await session.execute(select(User).where(User.id.in_(assignee_ids)))).scalars()
        assignees = {row.id: row for row in rows}

    out_lists: list[ListView] = []
    for plist, access in visible:
        full_read = access in _FULL_READ_ACCESS
        item_views: list[ItemView] = []
        for item in items_by_list.get(plist.id, []):
            assigned = item.assigned_to == actor.id
            if mode == "assigned" and not assigned:
                continue
            if not full_read and not assigned:
                continue
            assignee = assignees.get(item.assigned_to) if item.assigned_to is not None else None
            item_views.append(
                ItemView(
                    id=item.id,
                    list_id=item.list_id,
                    name=item.name,
                    quantity=practical_number(item.quantity) or 0,
                    unit=item.unit,
                    category=item.category,
                    status=item.status,
                    planned_for=item.planned_for,
                    price_amount=practical_number(item.price_amount),
                    price_currency=item.price_currency,
                    price_kind=item.price_kind,
                    assigned_to=item.assigned_to,
                    assignee_name=assignee.display_name if assignee is not None else None,
                )
            )
        if mode == "assigned" and not item_views:
            continue
        owner = owners.get(plist.owner_id)
        out_lists.append(
            ListView(
                id=plist.id,
                owner_id=plist.owner_id,
                owner_name=owner.display_name if owner is not None else str(plist.owner_id),
                store_key=plist.store_key,
                store_name=plist.store_name,
                title=_title(plist),
                access=access,
                items=item_views,
            )
        )
    return QueryResult(out_lists, _context_view(context))
