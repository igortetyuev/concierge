---
id: "001"
title: "Stage 1: Checklist backend — show_checklist tool, submit endpoint, runtime/history"
status: done
prev_plan: ["2026-11-24-skills.md"]
next_plan: ["002-checklists-pwa.md", "003-checklists-telegram.md"]
devin_session_id: energetic-armadillo
resources: []
touches:
  - server/src/concierge/agent/tools.py
  - server/src/concierge/agent/runtime.py
  - server/src/concierge/agent/history.py
  - server/src/concierge/skills/checklists.py (new)
  - server/src/concierge/skills/choices.py
  - server/src/concierge/skills/tools.py
  - server/src/concierge/skills/activation.py
  - server/src/concierge/api/conversations.py
  - server/tests/test_checklists.py (new)
---

# Stage 1: Checklist backend

## Goal & Scope
Add a server-side `show_checklist` agent tool that appends a `checklist` part to an assistant message (list mode or table mode), plus a `POST /conversations/{cid}/messages/{mid}/checklist` endpoint that submits the checkbox state, posts a readable user answer message, and schedules a new run. Mirrors the existing `ask_user_question` → `choice` → `/answer` → `answer_choice` flow. Also fixes two gaps in the existing `choice` flow that the checklist flow needs (and, optionally, applies the same fix to `choice`).

No DB migration: checklists live in `messages.parts` (JSONB).

## Fixed contract (freeze before S2/S3 start)
- Part: `{"type":"checklist","checklist_id":"<32hex>","title":"","columns":[],"items":[{"id","text","cells":[],"checked":false}],"submit_label":"Отправить","skill":"school"|null,"submissions":0,"submitted_by":null,"submitted_at":null}`. `columns==[]` → list mode. `items[].checked` holds starting state, then last submitted state.
- Endpoint `POST /conversations/{cid}/messages/{mid}/checklist`, body `{"checklist_id": str, "checked": [item_id]}` → 201 MessageOut. Errors: 404 (message/list/membership), 409 (archived), 422 (unknown item id).
- User answer part: `{"type":"text","text":"Отмечено в «…»: ☑… ☐…","checklist_answer":{"message_id","checklist_id","skill","items":[{"id","checked"}],"changed":[{"id","checked"}]}}`. `client_msg_id = cl:{mid}:{n}:{checklist_id[:8]}` for dedup.

## Step-by-Step Checklist
- [x] Tool in `agent/tools.py`: `ToolDeps.checklists` + `skill_name`; `ChecklistItem` + `show_checklist(ctx, items, title="", columns=None, submit_label="Отправить")` after `ask_user_question` (~:676-732). Validation: 1–50 items, one mode only, `len(cells)==len(columns)`, ≤6 columns, ids default `"1".."n"`, unique & ≤64 chars, one checklist per run.
- [x] Register tool: add `show_checklist` to `DYNAMIC_TOOLS` (~:864) and `SUBAGENT_EXCLUDED` (~:743); register next to `ask_user_question` in `runtime.py` (~:262-270), skipping voice sessions / `voice_quick`.
- [x] Runtime wiring: set `ToolDeps.skill_name` (~:294) from `ephemeral_state`/`active`; `_save_reply` (~:855-878) appends `deps.checklists` after `choice` parts; `_content` (~:785-794) skips `checklist` parts and, for a `checklist_answer` text part, adds a prompt-only `[чек-лист: {json ids/changed}]` line.
- [x] Answer-run reactivation: in `_run` (after command block ~:150-164), if trigger has `checklist_answer.skill`, resolve that skill for the submitter; if ephemeral set `ephemeral_state`; set `activated=True` (skips phrase triggers, wakes mention-mode groups).
- [x] History/prompt: `render_for_summary` (~:353) → `[чек-лист] title (N пунктов)`; `SKILL_COMPAT_PREAMBLE` adds «todo-лист / чек-лист с галочками → `show_checklist`».
- [x] New `skills/checklists.py`: `ChecklistError`, `render_state()`, `submit_checklist(...)`; extract shared `publish_answer(conv_id, msg, reply)` from `choices.answer_choice` tail (~:93-111) and call from both.
- [x] Endpoint in `api/conversations.py` next to `/answer` (~:551-587).
- [x] Apply the same reactivation fix to `choice` (per user decision): record the active skill on the `choice` part, make `answer_choice` re-apply an ephemeral skill and count the answer as a mention (wake `mention`-mode groups); update `choice` tests.
- [x] Tests: new `tests/test_checklists.py` (reuse `test_skill_runtime.py` helpers) — part appended last; validation errors; second call refused; endpoint posts answer w/ correct `changed`, updates part, schedules run; resubmit increments `submissions`; 404/409/422; follow-up run prompt contains `[чек-лист:`; ephemeral skill instructions reappear in answer run; answer wakes `mention` group; tool not in `NAMED_MODEL_TOOLS`/subagent tools.

## Acceptance Criteria & Unit Tests
- [x] `cd server && uv run ruff check . && uv run mypy src && uv run pytest tests/test_checklists.py tests/test_skill_runtime.py` — all green.
- [x] Tool call produces a `checklist` part as the last part of the reply; submitting returns 201 and triggers a new run that can read the checkbox state.
- [x] `choice` answers now wake `mention` groups and re-apply an ephemeral skill (existing choice tests updated and passing).

## Out of scope
Telegram UI (S3), PWA widget (S2), prod deploy & `school` skill (S4).
