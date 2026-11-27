---
id: "006"
title: "Stage 2: Purchase agent tools, default skill, checklist flow, spoken override"
status: done
prev_plan: ["005-purchases-domain.md"]
next_plan: ["007-purchases-validation-deploy.md"]
resources: ["alembic-head", "agent-runtime", "tool-registry", "tts-behavior"]
files:
  - server/src/concierge/purchases/tools.py
  - server/src/concierge/purchases/default_skill.py
  - server/alembic/versions/0019_purchase_skill.py
  - server/src/concierge/agent/tools.py
  - server/src/concierge/agent/runtime.py
  - server/src/concierge/media/tts.py
  - server/src/concierge/channels/telegram/sink.py
  - server/src/concierge/api/conversations.py
  - server/tests/test_purchase_tools.py
  - server/tests/test_tts.py
  - server/tests/test_skill_runtime.py
agent_ids:
  coder: 711ab78a
  tester: 2d911b80
---

# Stage 2: Purchase agent integration

## Goal & Scope

Connect the stage-005 purchase domain to the agent and existing channels:

- type-safe purchase tools;
- a shared default `purchases` skill visible to all roles in the skill catalog;
- natural purchase-list interaction using markdown plus the existing checklist mechanism;
- deterministic spoken-list rendering that differs from visual markdown where appropriate.

No purchase-specific PWA/Telegram UI and no new purchase-specific REST API are in scope for MVP. Production deployment is not in scope for this stage.

## Environment prerequisites

- Stage 005 is `done` and its tests pass.
- `cd server && uv sync` has been run or dependencies are already available.
- Unit tests use existing test fixtures; no live Ollama/qwen3-vl/Telegram/TTS service is required.

## Agent tools

Add purchase tools following the existing registration/gating patterns in `server/src/concierge/agent/tools.py` and schedule/checklist tool examples.

### `purchase_update`

Batch mutation tool. Recommended operation input shape:

```python
class PurchaseOperation(BaseModel):
    action: Literal[
        "add", "set", "increase", "decrease",
        "multiply", "divide", "remove",
        "mark_bought", "mark_pending",
    ]
    item_id: int | None = None
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
```

The tool accepts `operations: list[PurchaseOperation]` and returns structured per-operation successes/errors/candidates. Rules:

- omitted `store` + `general=False` uses current store context;
- `general=True` explicitly targets the general list;
- omitted `planned_for` means today;
- resolve assignee names using the same active-user resolution approach as existing invite/forward tools; if helper reuse requires refactoring, keep the refactor minimal;
- ambiguous users/items/lists return candidates suitable for `ask_user_question`;
- validation errors are structured and never raw tracebacks.

### `purchase_query`

Recommended signature:

```python
async def purchase_query(
    ctx,
    store: str | None = None,
    general: bool = False,
    all_stores: bool = False,
    status: Literal["pending", "bought", "all"] = "pending",
    for_date: date | None = None,
    include_future: bool = False,
) -> dict[str, Any]
```

Returns structured lists plus `markdown`, `spoken_text`, and `store_context`. It must also make the deterministic spoken rendering available to the runtime through the spoken override mechanism described below.

### `purchase_store`

Input:

- `action: Literal["set", "clear", "current"]`
- `store: str | None = None`

Semantics:

- «я в пятерочке» → set;
- «я ушел/ушла» → clear;
- «где я» / context query → current.

### `purchase_share`

Input:

- `users: list[str]`
- `store: str | None = None`
- `general: bool = False`
- `mode: Literal["viewer", "editor"] = "editor"`
- `revoke: bool = False`

Only the owner may share/unshare. Ambiguous user names return candidates. User-approved default mode is `editor`. For family users this primarily upgrades implicit read-only visibility to edit access; for guests it is the only way to grant access.

### Tool gating

- Register tools consistently with existing core tools.
- Built-in roles with `tools.allow=["*"]` should receive them automatically.
- Guests have no implicit family read access and the built-in guest role keeps `tools.deny=["*"]`. The skill may still be visible in the catalog because `role_ids=[]`, but effective purchase tool use by guests is denied unless an administrator deliberately changes role permissions and a list is explicitly shared.
- Add mutation tools (`purchase_update`, `purchase_share`, `purchase_store`) to subagent exclusions if the existing `SUBAGENT_EXCLUDED` mechanism applies. `purchase_query` may remain available to subagents.

## Default shared skill

Create `server/src/concierge/purchases/default_skill.py` containing the complete SKILL.md text for purchase-list behavior.

Create alembic migration `0019_purchase_skill.py` that insert-if-absent seeds a shared skill:

- `owner_id=NULL` shared skill;
- `role_ids=[]` so it is visible to all roles in skill visibility semantics;
- `enabled=true`;
- `ephemeral=true` so one-off shopping phrases do not permanently pin shopping mode;
- name `purchases` plus aliases such as `покупки` and possibly `купить`;
- does not overwrite an existing row if already customized.

Minimum trigger phrases:

- `надо купить`, `нужно купить`
- `надо еще`, `нужно еще`
- `добавь в список покупок`, `добавить в список покупок`
- `список покупок`
- `закончились`, `закончился`, `закончилась`, `закончилось`
- `что осталось купить`, `что еще осталось купить`
- `я в пятерочке`, `я в спаре`
- `я ушел`, `я ушла`
- `поделись списком`
- `должен купить`

Even when no trigger matches, the skill remains available through the existing catalog/`use_skill` mechanism.

### Skill responsibilities

Put as much configurable behavior as practical into the skill text:

- extraction examples:
  - «не два, а один» → `set quantity=1`
  - «в два раза больше» → `multiply factor=2`
  - «в полтора раза меньше» → `divide factor=1.5`
  - «не надо» → `remove`
- canonical product form: concise nominative form without filler words;
- unit inference and normalization expectations, including open-ended measures;
- category taxonomy: овощи/фрукты, молочные, мясо/рыба, бакалея, напитки, хлеб/выпечка, заморозка, бытовая химия, аптека, зоотовары, прочее;
- store canonicalization (`спар` → `SPAR`, `пятерочка`/`пятёрочка` → `Пятёрочка`);
- price interpretation:
  - «по 80 рублей» → `price_kind=unit`
  - «за 150 рублей» → `price_kind=total`
- conversion of relative dates (`завтра`, `послезавтра`, weekdays) using `[Сейчас]` context;
- formatting/query rules;
- ambiguity handling through `ask_user_question`;
- checklist behavior and item-id based state changes;
- sharing and assignment examples.

qwen3-vl is not required for MVP. The main chat model performs extraction/classification as part of structured tool invocation; server-side code normalizes and deduplicates deterministically.

## Checklist integration

Use the existing `show_checklist` flow rather than new client UI:

- for non-voice «что осталось купить»/list queries, the skill should call `purchase_query` first;
- if pending items are <= 50, call `show_checklist`;
- checklist item ids must be stable string forms of numeric `purchase_items.id`;
- `checked` reflects bought state;
- changed checked items map to `purchase_update` with exact `item_id`:
  - checked → `mark_bought`
  - unchecked → `mark_pending`
- more than 50 items falls back to markdown;
- checklist is a snapshot, not a live view; stale ids return structured errors rather than corrupting state.

Do not change PWA or Telegram checklist rendering unless a defect in the existing shared mechanism blocks this integration.

## Spoken override

Implement a generic spoken override mechanism so purchase lists can be read differently from their visual rendering:

1. Add `spoken_text: str | None = None` or equivalent to the existing tool dependency/context object.
2. `purchase_query` stores deterministic spoken rendering there when returning a list.
3. Before saving an assistant reply, add metadata to the final text part when present, for example `{"type":"text","text": display, "spoken": spoken_text}`. Visual text must remain unchanged.
4. Add a helper in `media/tts.py` such as `spoken_override(parts) -> str | None`.
5. Automatic voice replies (`voice_quick`, `voice_session`, Telegram voice reply) use the override when present.
6. Manual `POST /conversations/{cid}/messages/{mid}/speak` also uses the override when present.
7. Persisted message parts must retain the extra `spoken` key; use existing part copying behavior rather than introducing a parallel storage mechanism.

Keep this mechanism generic; do not hard-code purchase-specific logic into TTS.

## Step-by-Step Checklist

- [x] Inspect existing agent tool registration, skill seeding/migration, checklist, TTS and voice-reply patterns before editing.
- [x] Implement `server/src/concierge/purchases/tools.py` with typed operation/result models and thin service calls.
- [x] Register `purchase_update`, `purchase_query`, `purchase_store`, `purchase_share` in `agent/tools.py` with correct gating/subagent exclusions.
- [x] Implement shared user-name resolution for assign/share operations, reusing existing logic where practical.
- [x] Implement `purchases/default_skill.py` with complete Russian skill instructions and trigger/alias metadata required by the existing skill parser.
- [x] Implement alembic migration `0019_purchase_skill.py` insert-if-absent with correct `down_revision` from `0018_purchase_lists` and safe downgrade that removes only the seeded row when appropriate.
- [x] Implement generic spoken override plumbing in tool deps/runtime/media/telegram/manual speak paths.
- [x] Add unit tests in `server/tests/test_purchase_tools.py` for all tool behavior and permissions.
- [x] Extend or add tests for spoken override in `server/tests/test_tts.py` and relevant runtime/conversation tests.
- [x] Extend skill tests for parsing, visibility, triggers and ephemeral behavior.
- [x] Run `cd server && uv run ruff check .` and fix findings.
- [x] Run `cd server && uv run mypy src` and fix findings.
- [x] Run `cd server && uv run pytest tests/test_purchase_tools.py tests/test_skill_runtime.py` plus targeted TTS/runtime tests and fix failures.

## Acceptance Criteria & Unit Tests

Acceptance criteria:

- Tools are registered and obey role gating; adult/child/speaker wildcard roles can use them, while guest use remains denied by the built-in role unless deliberately reconfigured.
- `purchase_query` returns structured data, markdown and spoken text, and sets the spoken override.
- Assistant messages containing purchase lists persist `spoken` metadata without changing visual rendering.
- Voice quick/session/Telegram/manual speak use the override where applicable.
- The seeded shared skill parses, is enabled, shared, ephemeral, visible according to `role_ids=[]`, and has representative triggers.
- Checklist-based item status changes use stable item ids and existing checklist transport.
- Full server checks are green.

Expected unit tests in `server/tests/test_purchase_tools.py`:

1. `test_tools_registered_and_gated`
2. `test_purchase_update_tool_add_and_query`
3. `test_purchase_update_batch_correction`
4. `test_item_id_mark_bought_and_pending`
5. `test_unknown_bought_uses_store_context`
6. `test_purchase_store_set_clear_current`
7. `test_share_tool_requires_owner`
8. `test_share_resolve_ambiguous_user`
9. `test_assignment_visible_to_assignee`
10. `test_query_returns_markdown_and_spoken`
11. `test_mutation_tools_excluded_from_subagents`

TTS/runtime tests:

- spoken override helper extraction;
- spoken override saved on text part;
- automatic voice reply uses override;
- manual speak uses override when present.

Skill tests:

- default skill parses;
- representative triggers match;
- shared visibility semantics through `available_skills`.

## Parallelism Notes

This stage is serialized after stage 005 and before stage 007. It owns the next alembic migration, central runtime/tool integration and shared TTS behavior. It is not parallel-safe with other purchase stages.
