---
id: "005"
title: "Stage 1: Purchase lists domain — schema, units, service, permissions"
status: done
prev_plan: ["004-school-checklist.md"]
next_plan: ["006-purchases-agent.md"]
resources: ["alembic-head", "orm-registry"]
files:
  - server/src/concierge/purchases/__init__.py
  - server/src/concierge/purchases/models.py
  - server/src/concierge/purchases/units.py
  - server/src/concierge/purchases/service.py
  - server/src/concierge/purchases/render.py
  - server/src/concierge/core/models.py
  - server/alembic/versions/0018_purchase_lists.py
  - server/tests/test_purchases.py
agent_ids:
  coder: 0acf7f2d
  tester: aa94cde7
---

# Stage 1: Purchase lists domain

## Goal & Scope

Create a first-class, LLM-independent purchase-list domain:

- persistent purchase lists keyed by owner and store;
- items with quantity, unit, category, planned date, status, price, assignment and audit fields;
- current-store context per user;
- sharing and assignment permissions;
- deterministic normalization, unit conversion, deduplication, mutation operations and markdown/spoken rendering.

This stage must not wire agent tools, channels, PWA, Telegram, TTS, or production deployment. Those belong to later stages.

User-approved product decisions:

1. Lists are personal and owned, but visible read-only to family users by default. Explicit sharing grants edit access; this preserves the meaning of «поделись списком с Настей и Тихоном».
2. Sharing defaults to `editor` unless the user explicitly says read-only.
3. A purchase without an explicit date is planned for today; overdue pending items remain visible; future items are hidden from today queries until their date arrives.
4. Store context persists until «я ушел/ушла», another store is set, it is explicitly cleared, or it expires after approximately 4 hours.
5. Guests are not family users and have no implicit access. Built-in guest role also keeps `tools.deny=["*"]`; a guest can use purchase data only if an administrator deliberately changes role/tool permissions and the list is explicitly shared.
6. MVP UX is markdown plus the existing checklist mechanism; no purchase-specific REST UI or realtime client.

## Environment prerequisites

- Python >= 3.12 and `uv` installed.
- `cd server && uv sync` has been run or dependencies are already available.
- Unit tests use the existing SQLite/in-memory test setup; no PostgreSQL, Qdrant, Ollama, Telegram or TTS service is required.

## Data model

Implement ORM models and alembic migration `0018_purchase_lists.py` for these tables.

### `purchase_lists`

One persistent row per `(owner, store)`; empty `store_key` means the general/non-store list.

- `id` integer primary key
- `owner_id FK users.id NOT NULL`
- `store_key String(128) NOT NULL DEFAULT ""`
- `store_name String(128) NULL`
- `created_at`, `updated_at`
- `UNIQUE(owner_id, store_key)`

No separate title field: display title is derived as «Покупки» or «Купить в {store_name}».

### `purchase_list_members`

- composite PK `(list_id, user_id)`
- `list_id FK purchase_lists.id ON DELETE CASCADE`
- `user_id FK users.id ON DELETE CASCADE`
- `role String(16) NOT NULL` — `viewer` or `editor`
- `added_by FK users.id NULL`
- `created_at`

The owner has full implicit rights and does not need a member row.

### `purchase_items`

- `id` integer primary key
- `list_id FK purchase_lists.id ON DELETE CASCADE`
- `name String(256) NOT NULL` — human display/canonical nominative form
- `canonical_key String(256) NOT NULL` — lowercase, ё→е, normalized punctuation/whitespace/product synonyms where deterministic
- `category String(64) NOT NULL DEFAULT "прочее"`
- `quantity Numeric(12,3) NOT NULL`
- `unit String(32) NOT NULL`
- `unit_dimension String(48) NOT NULL` — `count`, `mass`, `volume`, `length`, or `unit:<custom>`
- `status String(16) NOT NULL` — `pending` or `bought`
- `planned_for Date NOT NULL`
- `price_amount Numeric(12,2) NULL`
- `price_currency String(8) NOT NULL DEFAULT "RUB"`
- `price_kind String(8) NULL` — `unit` or `total`
- `assigned_to FK users.id NULL`
- `assigned_by FK users.id NULL`
- `added_by FK users.id NULL`
- `bought_by FK users.id NULL`
- `bought_at DateTime NULL`
- `created_at`, `updated_at`

Indexes: `(list_id, status)`, `(canonical_key)`, `(assigned_to)`, `(planned_for)`.

### `purchase_contexts`

- `user_id PK FK users.id ON DELETE CASCADE`
- `store_key String(128) NOT NULL`
- `store_name String(128) NOT NULL`
- `updated_at`

## Domain semantics

### `purchases/units.py`

Implement deterministic unit normalization and conversion:

- aliases include at least:
  - `шт/штука/штуки` → `шт`
  - `г/гр/грамм/граммы` → `г`
  - `кг/килограмм/килограммы` → `кг`
  - `мл/миллилитр/миллилитры` → `мл`
  - `л/литр/литры` → `л`
  - `мм/миллиметр/миллиметры` → `мм`
  - `см/сантиметр/сантиметры` → `см`
  - `м/метр/метры` → `м`
  - `бутылка/бутылки`, `упаковка/упаковки`, `пачка/пачки` and similarly common packaging units
- unknown/open-ended units are preserved as custom units rather than rejected;
- conversion is allowed only inside one dimension:
  - mass: `г ↔ кг`
  - volume: `мл ↔ л`
  - length: `мм ↔ см ↔ м`
- count/package units do not convert into each other;
- use `Decimal` internally where needed to avoid binary floating point artifacts; expose practical numeric values for the service API.

### `purchases/service.py`

Implement:

- product key normalization;
- store key/name normalization (for example `спар` → canonical `SPAR`, `пятерочка`/`пятёрочка` → `Пятёрочка`);
- current context get/set/clear with a 4-hour expiry derived from `updated_at`;
- access control:
  - owner can read/mutate/share their lists;
  - non-guest family users can read all family members' lists by default, but cannot mutate them without explicit editor access;
  - explicit member `editor` can read/mutate but not share/unshare unless already owner;
  - explicit member `viewer` can read; this matters primarily for guests because family users already have implicit read access;
  - assignee can see and mark/update only items assigned to them in a list they otherwise could not mutate;
  - guests have no implicit access;
- mutation target resolution:
  - explicit store wins over current context;
  - `general=True` selects the caller's own general list;
  - the caller's own list is preferred where applicable;
  - exactly one explicit editable shared list may be a fallback target;
  - family-visible read-only lists are never mutation targets merely because they are visible;
  - multiple candidates produce a structured ambiguity result, not an exception traceback;
- atomic batch operations within one service call where practical;
- list-row locking or an equivalent transaction pattern to serialize concurrent add/dedupe where supported.

### Operation semantics

Support these low-level operations for later agent tools:

- `add` — add or merge with a pending item;
  - merge key: `canonical_key + unit_dimension + planned_for + assignee` within the target list;
  - mixed units in one dimension convert into the existing item's unit (`500 г` + `0.5 кг` merges correctly);
- `set` — exact correction («не два, а один»);
- `increase` / `decrease` — delta operations;
- `multiply` / `divide` — factor operations;
- `remove` — remove a pending item;
- `mark_bought`:
  - existing pending item becomes `bought`, with `bought_by`, `bought_at`, and optional price update;
  - if item is not found, create it already as bought in the caller's own explicit/context-store list, otherwise in the caller's own general list;
- `mark_pending` — restore a bought item to pending.

Default missing quantity is `1`; default missing unit is `шт`, unless a later skill/tool supplies a better inference.

### Date semantics

- `planned_for` defaults to today's date in the family/application timezone pattern already used by scheduler code.
- Today queries include `planned_for <= today` so overdue pending items do not disappear.
- Future-dated items are excluded unless `include_future=True`.
- Exact date parsing from Russian phrases belongs to the agent/skill stage, not this stage.

### Query/render semantics

Implement structured query plus rendering helpers:

- query modes cover the caller's own lists, items assigned to the caller, and all family-visible lists; exact filtering belongs to the service API and later `purchase_query` tool;
- lists with owner/store/access flags (`owner`, `editor`, `viewer`/read-only family access, assignee-only);
- items with id/name/quantity/unit/category/status/planned_for/price/assignee;
- current unexpired store context;
- `markdown` grouped by store/list with sections «Осталось купить» and «Уже купили»;
- `spoken_text` in a conversational form such as «В Пятёрочке уже купили хлеб и молоко. Осталось купить два килограмма яблок и литр сока.»

Do not implement natural-language quantity inflection beyond practical common forms; correctness and clarity are more important than perfect Russian morphology.

## Step-by-Step Checklist

- [x] Create `server/src/concierge/purchases/` package skeleton.
- [x] Implement ORM models in `purchases/models.py` and register/import them through `server/src/concierge/core/models.py` following existing model registration conventions.
- [x] Implement alembic migration `0018_purchase_lists.py` with correct `down_revision` from `0017_hidden_messages` and full downgrade.
- [x] Implement unit aliases, dimensions and conversion helpers in `purchases/units.py`.
- [x] Implement product/store normalization helpers.
- [x] Implement list access/sharing/assignment permission logic in `purchases/service.py`.
- [x] Implement current-store context set/get/clear.
- [x] Implement target-list resolution including explicit store, context, general list, shared editable fallback and ambiguity candidates.
- [x] Implement add/set/increase/decrease/multiply/divide/remove/mark_bought/mark_pending operations.
- [x] Implement query APIs with status/date/store filters and structured results.
- [x] Implement deterministic markdown and spoken rendering in `purchases/render.py`.
- [x] Add unit tests in `server/tests/test_purchases.py` covering every acceptance criterion.
- [x] Run `cd server && uv run ruff check .` and fix findings.
- [x] Run `cd server && uv run mypy src` and fix findings.
- [x] Run `cd server && uv run pytest tests/test_purchases.py` and fix failures.

## Acceptance Criteria & Unit Tests

Acceptance criteria:

- Alembic migration 0018 creates and drops all purchase tables cleanly.
- Test schema creation through existing `Base.metadata.create_all`/test conventions works.
- Service permissions allow non-guest family users to read but not mutate other family members' lists; guests cannot read unshared lists; explicit viewers cannot mutate; assignees cannot see unrelated items in lists they otherwise could not access.
- Future purchases are absent from default/today queries; overdue pending purchases remain visible.
- Unknown bought items are created as bought in the caller's own correct explicit/context/general list.
- Duplicate products merge where appropriate and do not merge across incompatible dimensions.
- Prices, categories, assignment and store metadata survive round trips.

Expected unit tests in `server/tests/test_purchases.py`:

1. `test_unit_normalization_and_conversion`
2. `test_store_and_product_key_normalization`
3. `test_add_dedupes_same_item`
4. `test_mixed_units_merge_correctly`
5. `test_different_dimensions_do_not_merge`
6. `test_set_increase_decrease_multiply_divide_remove`
7. `test_mark_bought_unknown_creates_item`
8. `test_mark_pending_restores_item`
9. `test_future_items_hidden_from_today`
10. `test_overdue_items_remain_visible`
11. `test_store_context_set_clear_and_expiry`
12. `test_explicit_store_overrides_context`
13. `test_family_read_only_and_guest_denied`
14. `test_share_editor_viewer_permissions`
15. `test_assignment_visibility_without_full_share`
16. `test_ambiguous_shared_list_returns_candidates`
17. `test_markdown_and_spoken_rendering`

## Parallelism Notes

This stage is serialized before stage 006. It owns the linear alembic head, ORM registry and all files under `server/src/concierge/purchases/`. It is not file-disjoint or resource-disjoint from stage 006 because stage 006 builds on these models and adds the next migration.
