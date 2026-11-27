# ruff: noqa: RUF001
"""Purchase-list domain: units, service permissions, dates, targets, rendering."""

import importlib.util
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select

from concierge.auth.models import Role, User
from concierge.core import db
from concierge.core.db import Base, utcnow
from concierge.purchases import render, service
from concierge.purchases.models import PurchaseContext, PurchaseItem, PurchaseListMember
from concierge.purchases.units import (
    convert_quantity,
    normalize_unit,
    unit_dimension,
)

from .conftest import make_user


def test_unit_normalization_and_conversion() -> None:
    assert normalize_unit("штуки") == "шт"
    assert normalize_unit("граммы") == "г"
    assert normalize_unit("килограммов") == "кг"
    assert normalize_unit("миллилитры") == "мл"
    assert normalize_unit("литра") == "л"
    assert normalize_unit("сантиметров") == "см"
    assert normalize_unit("бутылки") == "бутылка"
    assert normalize_unit("пучок") == "пучок"
    assert unit_dimension("кг") == "mass"
    assert unit_dimension("л") == "volume"
    assert unit_dimension("м") == "length"
    assert unit_dimension("бутылка") == "count"
    assert unit_dimension("пучок") == "unit:пучок"
    assert convert_quantity(Decimal("0.5"), "кг", "г") == Decimal("500")
    assert convert_quantity(Decimal("1.5"), "л", "мл") == Decimal("1500.0")
    assert convert_quantity(Decimal("20"), "мм", "см") == Decimal("2")
    assert convert_quantity(Decimal("1"), "кг", "л") is None
    assert convert_quantity(Decimal("1"), "шт", "бутылка") is None


def test_store_and_product_key_normalization() -> None:
    assert service.normalize_store(" спар ") == ("spar", "SPAR")
    assert service.normalize_store("Пятёрочка") == ("pyaterochka", "Пятёрочка")
    assert service.normalize_store("пятерочка") == ("pyaterochka", "Пятёрочка")
    assert service.normalize_store("Мой магазин") == ("мой магазин", "Мой магазин")
    assert service.normalize_product_key("  КАРТОШКА!!! ") == "картофель"
    assert service.normalize_product_key("Молоко   3,2%") == "молоко 3 2"
    assert service.normalize_product_key("Ёлочные игрушки") == "елочные игрушки"


def test_migration_0018_upgrade_downgrade(tmp_path: Path) -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0018_purchase_lists.py"
    spec = importlib.util.spec_from_file_location("migration_0018", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine(f"sqlite:///{tmp_path}/migration.db")
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[Role.__table__, User.__table__])
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()
        conn.commit()
    names = set(inspect(engine).get_table_names())
    assert {
        "purchase_lists",
        "purchase_list_members",
        "purchase_items",
        "purchase_contexts",
    } <= names
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.downgrade()
        conn.commit()
    names = set(inspect(engine).get_table_names())
    assert "purchase_lists" not in names
    assert "purchase_list_members" not in names
    assert "purchase_items" not in names
    assert "purchase_contexts" not in names


async def _count_items(list_id: int) -> int:
    async with db.get_sessionmaker()() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseItem)
                    .where(PurchaseItem.list_id == list_id)
                )
            ).scalar_one()
        )


async def test_add_dedupes_same_item(engine) -> None:
    user = await make_user("buy_owner", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, user, "Молоко", 1, "шт", general=True)
        second = await service.add(session, user, " молоко! ", 2, "штуки", general=True)
    assert first.ok and second.ok
    assert first.item is not None and second.item is not None
    assert first.item.id == second.item.id
    assert first.item.planned_for == service.family_today()
    assert second.item.quantity == Decimal("3")
    assert await _count_items(first.item.list_id) == 1


async def test_add_many_atomic(engine) -> None:
    user = await make_user("buy_batch", "adult")
    async with db.get_sessionmaker()() as session:
        batch = await service.add_many(
            session,
            user,
            [
                service.PurchaseItemInput("Молоко", 1),
                service.PurchaseItemInput("молоко", 2),
                service.PurchaseItemInput("Хлеб", 1),
            ],
            general=True,
        )
        assert batch.ok and len(batch.results) == 3
        first, second, third = batch.results
        assert first.item is not None and second.item is not None and third.item is not None
        assert first.item.id == second.item.id
        assert first.item.quantity == Decimal("3")
        list_id = first.item.list_id
        failed = await service.add_many(
            session,
            user,
            [service.PurchaseItemInput("Чай", 1), service.PurchaseItemInput("!!!", 1)],
            general=True,
        )
        assert not failed.ok and failed.error == "invalid_name"
        items = (
            await session.execute(
                select(PurchaseItem).where(
                    PurchaseItem.list_id == list_id,
                    PurchaseItem.canonical_key.in_(["чай", "!!!"]),
                )
            )
        ).scalars().all()
        assert list(items) == []


async def test_mixed_units_merge_correctly(engine) -> None:
    user = await make_user("buy_mixed", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, user, "Сахар", 500, "г", general=True)
        second = await service.add(session, user, "сахар", Decimal("0.5"), "кг", general=True)
    assert first.item is not None and second.item is not None
    assert first.item.id == second.item.id
    assert second.item.unit == "г"
    assert second.item.quantity == Decimal("1000")
    assert await _count_items(first.item.list_id) == 1


async def test_unspecified_duplicate_add_is_noop_not_cross_dimension_row(engine) -> None:
    user = await make_user("buy_unspecified", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, user, "яблоки", 2, "кг", general=True)
        duplicate = await service.add(session, user, "Яблоки", general=True)
        blank_unit_duplicate = await service.add(
            session, user, "яблоки", None, " ", general=True
        )
        explicit_piece = await service.add(
            session, user, "яблоки", 1, "шт", general=True
        )
    assert first.item is not None and duplicate.item is not None
    assert duplicate.item.id == first.item.id
    assert duplicate.item.quantity == Decimal("2")
    assert duplicate.item.unit == "кг"
    assert blank_unit_duplicate.item is not None
    assert blank_unit_duplicate.item.id == first.item.id
    assert explicit_piece.item is not None
    assert not explicit_piece.ok
    assert explicit_piece.error == "unit_mismatch"
    assert explicit_piece.item.id == first.item.id
    assert await _count_items(first.item.list_id) == 1


async def test_unspecified_duplicate_add_merges_explicit_price_metadata(engine) -> None:
    user = await make_user("buy_price_duplicate", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, user, "молоко", 1, "л", general=True)
        duplicate = await service.add(
            session,
            user,
            "молоко",
            general=True,
            price_amount=80,
            price_currency="RUB",
            price_kind="unit",
        )
    assert first.item is not None and duplicate.item is not None
    assert duplicate.ok
    assert duplicate.item.id == first.item.id
    assert duplicate.item.quantity == Decimal("1")
    assert duplicate.item.unit == "л"
    assert duplicate.item.status == "pending"
    assert duplicate.item.price_amount == Decimal("80.00")
    assert duplicate.item.price_currency == "RUB"
    assert duplicate.item.price_kind == "unit"


async def test_cross_dimension_duplicate_is_unit_mismatch(engine) -> None:
    user = await make_user("buy_dims", "adult")
    async with db.get_sessionmaker()() as session:
        volume = await service.add(session, user, "Вода", 1, "л", general=True)
        mass = await service.add(session, user, "Вода", 1, "кг", general=True)
        bottle = await service.add(session, user, "Сок", 1, "бутылка", general=True)
        pack = await service.add(session, user, "Сок", 1, "упаковка", general=True)
    assert volume.item is not None and mass.item is not None
    assert not mass.ok
    assert mass.error == "unit_mismatch"
    assert mass.item.id == volume.item.id
    # Non-convertible packaging units share the count dimension and may still
    # coexist as distinct rows; only cross-dimension shadow duplicates are blocked.
    assert bottle.item is not None and pack.item is not None
    assert bottle.item.id != pack.item.id
    assert await _count_items(volume.item.list_id) == 3


async def test_allow_incompatible_unit_creates_separate_row_but_still_merges(
    engine,
) -> None:
    user = await make_user("buy_dims_opt_in", "adult")
    async with db.get_sessionmaker()() as session:
        mass = await service.add(session, user, "яблоки", 2, "кг", general=True)
        rejected = await service.add(session, user, "яблоки", 1, "шт", general=True)
        separate = await service.add(
            session,
            user,
            "яблоки",
            1,
            "шт",
            general=True,
            allow_incompatible_unit=True,
        )
        merged = await service.add(
            session,
            user,
            "яблоки",
            500,
            "г",
            general=True,
            allow_incompatible_unit=True,
        )
    assert mass.item is not None and separate.item is not None and merged.item is not None
    assert not rejected.ok and rejected.error == "unit_mismatch"
    assert separate.ok
    assert separate.item.id != mass.item.id
    assert separate.item.unit == "шт"
    assert merged.item.id == mass.item.id
    assert merged.item.quantity == Decimal("2.5")
    assert merged.item.unit == "кг"
    assert await _count_items(mass.item.list_id) == 2


async def test_set_increase_decrease_multiply_divide_remove(engine) -> None:
    user = await make_user("buy_ops", "adult")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, user, "Яблоки", 4, "шт", general=True)
        assert added.ok
        assert (await service.set_quantity(session, user, "яблоки", 2, general=True)).ok
        increased = await service.increase(session, user, "яблоки", 3, general=True)
        assert increased.ok and increased.item is not None
        assert increased.item.quantity == Decimal("5")
        decreased = await service.decrease(session, user, "яблоки", 2, general=True)
        assert decreased.ok and decreased.item is not None
        assert decreased.item.quantity == Decimal("3")
        multiplied = await service.multiply(session, user, "яблоки", 2, general=True)
        assert multiplied.ok and multiplied.item is not None
        assert multiplied.item.quantity == Decimal("6")
        divided = await service.divide(session, user, "яблоки", 4, general=True)
        assert divided.ok and divided.item is not None
        assert divided.item.quantity == Decimal("1.5")
        removed = await service.remove(session, user, "яблоки", general=True)
        assert removed.ok
        missing = await service.remove(session, user, "яблоки", general=True)
        assert not missing.ok and missing.error == "not_found"


async def test_mark_bought_unknown_creates_item(engine) -> None:
    user = await make_user("buy_unknown", "adult")
    async with db.get_sessionmaker()() as session:
        result = await service.mark_bought(
            session,
            user,
            "Хлеб",
            2,
            "шт",
            category="выпечка",
            assigned_to=user.id,
            price_amount="129.90",
            price_kind="total",
        )
        await service.set_context(session, user, "спар")
        context_bought = await service.mark_bought(session, user, "Кофе", 1)
        explicit_bought = await service.mark_bought(
            session, user, "Сок", 1, store="Пятёрочка"
        )
        roundtrip = await service.query(session, user, mode="own")
    assert result.ok and result.item is not None and result.purchase_list is not None
    assert result.item.status == "bought"
    assert result.item.bought_by == user.id
    assert result.item.bought_at is not None
    assert result.item.category == "выпечка"
    assert result.item.assigned_to == user.id
    assert result.item.price_amount == Decimal("129.90")
    assert result.item.price_currency == "RUB"
    assert result.item.price_kind == "total"
    assert result.purchase_list.store_key == ""
    assert context_bought.ok and context_bought.purchase_list is not None
    assert context_bought.purchase_list.store_key == "spar"
    assert explicit_bought.ok and explicit_bought.purchase_list is not None
    assert explicit_bought.purchase_list.store_key == "pyaterochka"
    view_items = {item.name: item for plist in roundtrip.lists for item in plist.items}
    assert view_items["Хлеб"].category == "выпечка"
    assert view_items["Хлеб"].price_amount == 129.9
    assert view_items["Хлеб"].price_kind == "total"
    assert roundtrip.context is not None and roundtrip.context.store_key == "spar"


async def test_mark_pending_restores_item(engine) -> None:
    user = await make_user("buy_restore", "adult")
    async with db.get_sessionmaker()() as session:
        bought = await service.mark_bought(session, user, "Сыр", 1, general=True)
        assert bought.ok and bought.item is not None
        restored = await service.mark_pending(session, user, bought.item.id)
    assert restored.ok and restored.item is not None
    assert restored.item.status == "pending"
    assert restored.item.bought_by is None
    assert restored.item.bought_at is None


async def test_future_items_hidden_from_today(engine) -> None:
    user = await make_user("buy_future", "adult")
    today = service.family_today()
    async with db.get_sessionmaker()() as session:
        added = await service.add(
            session, user, "Подарок", 1, planned_for=today + timedelta(days=1)
        )
        assert added.ok
        visible = await service.query(session, user, mode="own", today=today)
        future = await service.query(
            session, user, mode="own", include_future=True, today=today
        )
    assert all(not plist.items for plist in visible.lists)
    assert any(item.name == "Подарок" for plist in future.lists for item in plist.items)


async def test_overdue_items_remain_visible(engine) -> None:
    user = await make_user("buy_overdue", "adult")
    today = service.family_today()
    async with db.get_sessionmaker()() as session:
        added = await service.add(
            session, user, "Лампочка", 1, planned_for=today - timedelta(days=2)
        )
        assert added.ok
        result = await service.query(session, user, mode="own", today=today)
    names = [item.name for plist in result.lists for item in plist.items]
    assert "Лампочка" in names


async def test_store_context_set_clear_and_expiry(engine) -> None:
    user = await make_user("buy_context", "adult")
    now = utcnow()
    async with db.get_sessionmaker()() as session:
        context = await service.set_context(session, user, "спар", now)
        assert context.store_key == "spar"
        assert context.store_name == "SPAR"
        assert await service.get_context(session, user, now + timedelta(hours=3)) is not None
        row = await session.get(PurchaseContext, user.id)
        assert row is not None
        row.updated_at = now - timedelta(hours=5)
        await session.commit()
        assert await service.get_context(session, user, now) is None
        await service.set_context(session, user, "Пятёрочка", now)
        await service.clear_context(session, user)
        assert await service.get_context(session, user, now) is None


async def test_explicit_store_overrides_context(engine) -> None:
    user = await make_user("buy_override", "adult")
    async with db.get_sessionmaker()() as session:
        await service.set_context(session, user, "спар")
        result = await service.add(session, user, "Молоко", 1, store="Пятёрочка")
    assert result.ok and result.purchase_list is not None
    assert result.purchase_list.store_key == "pyaterochka"
    assert result.purchase_list.store_name == "Пятёрочка"


async def test_family_read_only_and_guest_denied(engine) -> None:
    owner = await make_user("buy_family_owner", "adult")
    family = await make_user("buy_family_reader", "child")
    guest = await make_user("buy_family_guest", "guest")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, owner, "Кофе", 1, general=True)
        assert added.ok and added.item is not None
        family_view = await service.query(session, family, mode="family")
        guest_view = await service.query(session, guest, mode="family")
        mutation = await service.increase(session, family, added.item.id, 1)
    family_items = [item for plist in family_view.lists for item in plist.items]
    assert any(item.name == "Кофе" for item in family_items)
    assert any(plist.access == "family" for plist in family_view.lists)
    assert not [item for plist in guest_view.lists for item in plist.items]
    assert not mutation.ok and mutation.error == "denied"


async def test_share_editor_viewer_permissions(engine) -> None:
    owner = await make_user("buy_share_owner", "adult")
    editor = await make_user("buy_share_editor", "adult")
    viewer = await make_user("buy_share_viewer", "guest")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, owner, "Чай", 1, general=True)
        assert added.ok and added.item is not None and added.purchase_list is not None
        list_id = added.purchase_list.id
        member = await service.share_list(session, owner, list_id, editor.id)
        assert member.role == "editor"
        viewer_member = await service.share_list(session, owner, list_id, viewer.id, "viewer")
        assert viewer_member.role == "viewer"
        edit = await service.increase(session, editor, added.item.id, 1)
        assert edit.ok
        fallback_add = await service.add(session, editor, "Сахар", 1)
        assert fallback_add.ok and fallback_add.purchase_list is not None
        assert fallback_add.purchase_list.id == list_id
        own_general = await service.add(session, editor, "Крупа", 1, general=True)
        assert own_general.ok and own_general.purchase_list is not None
        assert own_general.purchase_list.id != list_id
        assert own_general.purchase_list.owner_id == editor.id
        denied = await service.increase(session, viewer, added.item.id, 1)
        assert not denied.ok and denied.error == "denied"
        guest_view = await service.query(session, viewer, mode="family")
        assert any(item.name == "Чай" for plist in guest_view.lists for item in plist.items)
        with pytest.raises(service.PurchaseError):
            await service.share_list(session, editor, list_id, viewer.id, "editor")
        with pytest.raises(service.PurchaseError):
            await service.unshare_list(session, editor, list_id, viewer.id)
        assert await service.unshare_list(session, owner, list_id, viewer.id)
        after_unshare = await service.query(session, viewer, mode="family")
        assert not [
            item for plist in after_unshare.lists for item in plist.items if item.name == "Чай"
        ]


async def test_assignment_visibility_without_full_share(engine) -> None:
    owner = await make_user("buy_assign_owner", "adult")
    guest = await make_user("buy_assign_guest", "guest")
    async with db.get_sessionmaker()() as session:
        assigned = await service.add(
            session, owner, "Авокадо", 2, general=True, assigned_to=guest.id
        )
        unrelated = await service.add(session, owner, "Крупа", 1, general=True)
        assert assigned.ok and assigned.item is not None
        assert unrelated.ok and unrelated.item is not None
        view = await service.query(session, guest, mode="family")
        names = [item.name for plist in view.lists for item in plist.items]
        assert names == ["Авокадо"]
        assert view.lists[0].access == "assignee"
        bought = await service.mark_bought(session, guest, assigned.item.id)
        assert bought.ok
        denied = await service.increase(session, guest, unrelated.item.id, 1)
        assert not denied.ok and denied.error == "denied"


async def test_ambiguous_shared_list_returns_candidates(engine) -> None:
    owner_a = await make_user("buy_amb_owner_a", "adult")
    owner_b = await make_user("buy_amb_owner_b", "adult")
    editor = await make_user("buy_amb_editor", "child")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, owner_a, "Хлеб", 1, store="спар")
        second = await service.add(session, owner_b, "Хлеб", 1, store="спар")
        assert first.ok and first.purchase_list is not None
        assert second.ok and second.purchase_list is not None
        await service.share_list(session, owner_a, first.purchase_list.id, editor.id)
        await service.share_list(session, owner_b, second.purchase_list.id, editor.id)
        result = await service.add(session, editor, "Молоко", 1, store="спар")
    assert not result.ok
    assert result.error == "ambiguous"
    assert len(result.candidates) == 2
    assert {candidate.owner_id for candidate in result.candidates} == {owner_a.id, owner_b.id}


async def test_markdown_and_spoken_rendering(engine) -> None:
    user = await make_user("buy_render", "adult")
    async with db.get_sessionmaker()() as session:
        apples = await service.add(session, user, "яблоки", 2, "кг", store="Пятёрочка")
        bread = await service.add(session, user, "хлеб", 1, "шт", store="Пятёрочка")
        assert apples.ok and bread.ok and bread.item is not None
        bought = await service.mark_bought(session, user, bread.item.id)
        assert bought.ok
        result = await service.query(session, user, mode="own", store="Пятёрочка")
        markdown = render.render_markdown(result)
        spoken = render.render_spoken(result)
    assert "## Купить в Пятёрочка" in markdown
    assert "### Осталось купить" in markdown
    assert "### Уже купили" in markdown
    assert "- [ ] 2 кг яблоки" in markdown
    assert "- [x] 1 шт хлеб" in markdown
    assert "В Пятёрочке уже купили хлеб." in spoken
    assert "осталось купить 2 килограмма яблоки" in spoken


async def test_assigned_item_fallback_when_assignee_has_same_store_list(engine) -> None:
    owner = await make_user("assign_owner_same_store", "adult")
    assignee = await make_user("assign_tikhon_same_store", "adult")
    async with db.get_sessionmaker()() as session:
        own = await service.add(
            session, assignee, "хлеб", 1, store="Пятёрочка"
        )
        assigned = await service.add(
            session,
            owner,
            "молоко",
            1,
            "л",
            store="Пятёрочка",
            assigned_to=assignee.id,
        )
        assert own.ok and own.purchase_list is not None
        assert assigned.ok and assigned.item is not None
        assert assigned.purchase_list is not None
        assert assigned.purchase_list.owner_id == owner.id

        corrected = await service.set_quantity(
            session, assignee, "молоко", 2, store="Пятёрочка"
        )
        assert corrected.ok
        assert corrected.item is not None and corrected.item.id == assigned.item.id
        assert corrected.item.quantity == 2

        bought = await service.mark_bought(
            session, assignee, "молоко", store="Пятёрочка"
        )
        assert bought.ok
        assert bought.item is not None and bought.item.id == assigned.item.id
        assert bought.purchase_list is not None
        assert bought.purchase_list.owner_id == owner.id

        own_rows = (
            await session.execute(
                select(PurchaseItem).where(PurchaseItem.list_id == own.purchase_list.id)
            )
        ).scalars().all()
    assert {row.name for row in own_rows} == {"хлеб"}


async def test_quantity_and_price_upper_bounds_are_structured(engine) -> None:
    user = await make_user("buy_bounds", "adult")
    async with db.get_sessionmaker()() as session:
        too_much = await service.add(session, user, "молоко", 10_000_000_000)
        assert not too_much.ok and too_much.error == "invalid_quantity"
        too_expensive = await service.add(
            session, user, "хлеб", 1, price_amount=100_000_000_000
        )
        assert not too_expensive.ok and too_expensive.error == "invalid_quantity"

        near_limit = await service.add(session, user, "вода", 999_999_999)
        assert near_limit.ok
        overflow_merge = await service.add(session, user, "вода", 1)
        assert not overflow_merge.ok
        assert overflow_merge.error == "invalid_quantity"

        factor_seed = await service.add(session, user, "сок", 500_000_000)
        assert factor_seed.ok
        overflow_factor = await service.multiply(session, user, "сок", 3)
        assert not overflow_factor.ok
        assert overflow_factor.error == "invalid_quantity"


async def test_create_owned_list_retries_duplicate_insert(engine) -> None:
    user = await make_user("buy_list_retry", "adult")
    async with db.get_sessionmaker()() as session:
        existing = service.PurchaseList(
            owner_id=user.id,
            store_key="pyaterochka",
            store_name="Пятёрочка",
        )
        session.add(existing)
        await session.commit()

        # This is the same catch/retry path hit when another transaction wins
        # the first-create race after the initial ownership check.
        result = await service._create_owned_list_with_retry(
            session, user.id, "pyaterochka", "Пятёрочка"
        )
    assert result.id == existing.id
    assert result.store_name == "Пятёрочка"


async def test_create_context_retries_duplicate_insert(engine) -> None:
    user = await make_user("buy_context_retry", "adult")
    async with db.get_sessionmaker()() as session:
        existing = PurchaseContext(
            user_id=user.id,
            store_key="pyaterochka",
            store_name="Пятёрочка",
            updated_at=utcnow(),
        )
        session.add(existing)
        await session.commit()

    async with db.get_sessionmaker()() as session:
        result = await service._create_context_with_retry(
            session, user.id, "spar", "SPAR", utcnow()
        )
    assert result.user_id == user.id
    assert result.store_key == "pyaterochka"
    assert result.store_name == "Пятёрочка"


async def test_custom_unit_lengths_are_validated_before_writes(engine) -> None:
    user = await make_user("buy_unit_bounds", "adult")
    custom32 = "x" * 32
    custom33 = "x" * 33
    async with db.get_sessionmaker()() as session:
        valid = await service.add(session, user, "провода", 2, custom32)
        assert valid.ok and valid.item is not None
        assert valid.item.unit == custom32
        assert valid.item.unit_dimension == f"unit:{custom32}"
        assert len(valid.item.unit_dimension) <= 48

        bad_add = await service.add(session, user, "кабель", 1, custom33)
        assert not bad_add.ok and bad_add.error == "invalid_unit"
        bad_set = await service.set_quantity(
            session, user, valid.item.id, 3, custom33
        )
        assert not bad_set.ok and bad_set.error == "invalid_unit"
        bad_bought = await service.mark_bought(session, user, "неизвестное", 1, custom33)
        assert not bad_bought.ok and bad_bought.error == "invalid_unit"

        unchanged = await session.get(PurchaseItem, valid.item.id)
        assert unchanged is not None
        assert unchanged.quantity == 2
        assert unchanged.unit == custom32
        rows = (
            await session.execute(
                select(func.count(PurchaseItem.id)).where(
                    PurchaseItem.list_id == valid.purchase_list.id
                )
            )
        ).scalar_one()
    assert rows == 1


async def test_multiply_divide_quantities_use_schema_scale(engine) -> None:
    user = await make_user("buy_quantity_scale", "adult")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, user, "сахар", Decimal("1.234"))
        assert added.ok
        multiplied = await service.multiply(session, user, "сахар", Decimal("1.25"))
        assert multiplied.ok and multiplied.item is not None
        assert multiplied.item.quantity == Decimal("1.543")

        seed = await service.add(session, user, "вода", 1)
        assert seed.ok
        divided = await service.divide(session, user, "вода", 3)
        assert divided.ok and divided.item is not None
        assert divided.item.quantity == Decimal("0.333")


async def test_read_modify_write_mutations_keep_working_with_list_locks(
    engine, monkeypatch
) -> None:
    user = await make_user("buy_locked_mutations", "adult")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, user, "молоко", 2)
        assert added.ok and added.item is not None and added.purchase_list is not None
        item_id = added.item.id
        list_id = added.purchase_list.id

        original_lock = service._lock_list
        locked_lists: list[int] = []

        async def spy_lock(session, locked_list_id):
            locked_lists.append(locked_list_id)
            await original_lock(session, locked_list_id)

        monkeypatch.setattr(service, "_lock_list", spy_lock)
        increased = await service.increase(session, user, item_id, Decimal("0.5"))
        assert increased.ok and increased.item is not None
        assert increased.item.quantity == Decimal("2.500")
        decreased = await service.decrease(session, user, item_id, Decimal("0.25"))
        assert decreased.ok and decreased.item is not None
        assert decreased.item.quantity == Decimal("2.250")
        multiplied = await service.multiply(session, user, item_id, 2)
        assert multiplied.ok and multiplied.item is not None
        assert multiplied.item.quantity == Decimal("4.500")
        divided = await service.divide(session, user, item_id, 3)
        assert divided.ok and divided.item is not None
        assert divided.item.quantity == Decimal("1.500")
        corrected = await service.set_quantity(session, user, item_id, 4)
        assert corrected.ok and corrected.item is not None
        assert corrected.item.quantity == Decimal("4.000")
        bought = await service.mark_bought(session, user, item_id)
        assert bought.ok and bought.item is not None and bought.item.status == "bought"
        restored = await service.mark_pending(session, user, item_id)
        assert restored.ok and restored.item is not None
        assert restored.item.status == "pending"
    assert locked_lists == [list_id] * 7


async def test_mark_bought_unknown_rechecks_state_under_final_list_lock(
    engine, monkeypatch
) -> None:
    user = await make_user("buy_race_lock", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.mark_bought(session, user, "хлеб", 1, general=True)
        assert first.ok and first.item is not None and first.purchase_list is not None
        first_item_id = first.item.id
        list_id = first.purchase_list.id

        original_find = service._find_mutation_item
        original_lock = service._lock_list
        original_rows = service._items_in_list_for_state
        events: list[tuple[str, int | str]] = []

        async def stale_find(session_arg, user_arg, item_ref, **kwargs):
            # Simulate the losing transaction's pre-lock snapshot: another
            # transaction inserted the row after both initial lookups missed.
            if isinstance(item_ref, str):
                return service._ItemTarget(None, None, error="not_found")
            return await original_find(session_arg, user_arg, item_ref, **kwargs)

        async def spy_lock(session_arg, locked_list_id):
            events.append(("lock", locked_list_id))
            await original_lock(session_arg, locked_list_id)

        async def spy_rows(session_arg, plist, canonical, status):
            events.append(("lookup", status))
            return await original_rows(session_arg, plist, canonical, status)

        monkeypatch.setattr(service, "_find_mutation_item", stale_find)
        monkeypatch.setattr(service, "_lock_list", spy_lock)
        monkeypatch.setattr(service, "_items_in_list_for_state", spy_rows)
        raced = await service.mark_bought(session, user, "хлеб", 1, general=True)

        row_count = (
            await session.execute(
                select(func.count(PurchaseItem.id)).where(
                    PurchaseItem.list_id == list_id,
                    PurchaseItem.canonical_key == "хлеб",
                )
            )
        ).scalar_one()
    assert raced.ok and raced.item is not None
    assert raced.item.id == first_item_id
    assert events == [("lock", list_id), ("lookup", "pending"), ("lookup", "bought")]
    assert row_count == 1


async def test_create_list_member_retries_duplicate_insert(engine) -> None:
    owner = await make_user("buy_share_retry_owner", "adult")
    target = await make_user("buy_share_retry_target", "adult")
    async with db.get_sessionmaker()() as session:
        plist = service.PurchaseList(owner_id=owner.id, store_key="", store_name=None)
        session.add(plist)
        await session.flush()
        existing = PurchaseListMember(
            list_id=plist.id,
            user_id=target.id,
            role="editor",
            added_by=owner.id,
        )
        session.add(existing)
        await session.commit()
        list_id = plist.id

    async with db.get_sessionmaker()() as session:
        # Same conflict/retry path hit when another transaction wins the
        # duplicate-share race after the initial member lookup misses.
        result = await service._create_list_member_with_retry(
            session, list_id, target.id, "viewer", owner.id
        )
    assert (result.list_id, result.user_id) == (list_id, target.id)
    assert result.role == "editor"

    async with db.get_sessionmaker()() as session:
        repeated = await service.share_list(session, owner, list_id, target.id, "viewer")
    assert (repeated.list_id, repeated.user_id) == (list_id, target.id)
    assert repeated.role == "viewer"


async def test_price_uses_schema_scale(engine) -> None:
    user = await make_user("buy_price_scale", "adult")
    async with db.get_sessionmaker()() as session:
        added = await service.add(session, user, "сыр", 1, price_amount=89.999)
        assert added.ok and added.item is not None
        assert added.item.price_amount == Decimal("90.00")
        initial_result = await service.query(session, user, mode="own")
        bought = await service.mark_bought(
            session, user, added.item.id, price_amount=12.345
        )
        assert bought.ok and bought.item is not None
        assert bought.item.price_amount == Decimal("12.35")
        result = await service.query(session, user, mode="own")

    def cheese_view(query_result):
        return next(
            item
            for plist in query_result.lists
            for item in plist.items
            if item.name == "сыр"
        )

    # practical_number intentionally serializes integral decimals as int.
    assert cheese_view(initial_result).price_amount == 90
    assert cheese_view(result).price_amount == 12.35


async def test_spoken_rendering_disambiguates_multi_owner_lists(engine) -> None:
    owner_a = await make_user("spoken_owner_a", "adult")
    owner_b = await make_user("spoken_owner_b", "adult")
    async with db.get_sessionmaker()() as session:
        first = await service.add(session, owner_a, "хлеб", 1, store="Пятёрочка")
        second = await service.add(session, owner_b, "молоко", 1, store="Пятёрочка")
        assert first.ok and second.ok
        single = await service.query(session, owner_a, mode="own", store="Пятёрочка")
        family = await service.query(session, owner_a, mode="family", store="Пятёрочка")
        single_spoken = render.render_spoken(single)
        family_spoken = render.render_spoken(family)
    assert single_spoken == "В Пятёрочке осталось купить хлеб."
    assert f"В списке «{owner_a.display_name}» в Пятёрочке" in family_spoken
    assert f"В списке «{owner_b.display_name}» в Пятёрочке" in family_spoken
    assert "хлеб" in family_spoken and "молоко" in family_spoken
