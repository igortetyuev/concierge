---
id: "008"
title: "Stage 1: Homework checklist sync"
status: done
prev_plan: ["004-school-checklist.md"]
next_plan: ["009-deploy-homework-checklist-sync.md"]
resources: []
files:
  - server/src/concierge/skills/checklists.py
  - server/src/concierge/agent/runtime.py
  - server/tests/test_homework_checklist_sync.py
  - server/tests/test_checklists.py
  - server/tests/mcp_analytics_stub.py
  - AGENTS.md
agent_ids:
  coder: e55c29e4
  tester: 86a05583
---

# Stage 1: Homework checklist sync

## Goal & Scope

When a successful agent run writes `sandbox.school_homework_done` through the MCP tool `analytics_import_rows`, update already-rendered homework checklist checkboxes in the same conversation and notify open PWA clients with realtime `message.updated` events.

The intended trigger examples are free-text commands such as «сделана география» and «не сделана география» after homework has been shown as a checklist. The PWA already applies `message.updated` to open messages; the missing piece is deterministic server-side reconciliation of stored checklist parts after homework-done imports.

In scope:

- extract successful `analytics_import_rows` writes to `school_homework_done` from stored run parts;
- update matching `items[].checked` values in all matching visible assistant checklist messages in the same conversation;
- publish one `message.updated` per rewritten message;
- preserve checklist submission metadata and followups;
- unit/integration regression coverage and documentation.

Out of scope:

- PWA rendering changes (existing `message.updated` handling is expected to suffice);
- Telegram live-update behavior;
- DB migrations;
- production deployment;
- editing the production DB-stored `school` skill;
- a new model-called checklist mutation tool.

## Design decisions

- Use deterministic post-run reconciliation, not a prompt-driven tool. The agent already writes homework state through `analytics_import_rows`; the server can use the stored tool log as the source of truth.
- Accept an import only when the `tool_call` targets table `school_homework_done` in append mode and the matching non-retry/non-failed `tool_return` is a structured dict for `sandbox.school_homework_done` with `inserted == len(rows)` and `inserted > 0`.
- Extract only rows with a non-empty string `hw_id` and strict boolean `done`. If duplicate ids occur, process rows/calls in stored order and let the last extracted write win.
- Scan all visible assistant messages in the conversation, not just the recent tail, and update all matching checklist cards. The same logical homework item may appear in multiple cards.
- Lock candidate rows `FOR UPDATE` with `populate_existing=True`, then recheck and rewrite only `items[].checked`. Preserve `submissions`, `submitted_by`, `submitted_at`, `followups`, and all other part fields.
- Commit before publishing realtime events. Reconciliation failure must be logged but must not fail the user-facing agent run.

## Step-by-Step Checklist

- [x] Add `extract_school_homework_done_updates(parts)` to `server/src/concierge/skills/checklists.py` with strict success validation of `analytics_import_rows` call/return pairs.
- [x] Add `sync_school_homework_done_checklists(conversation_id, updates)` to `server/src/concierge/skills/checklists.py`: scan/lock/update matching checklist messages, commit, then publish `message.updated` for each changed message.
- [x] Wire post-run reconciliation into `server/src/concierge/agent/runtime.py` after `_save_reply` in the success path; catch/log reconciliation errors. Consider the failure path too if complete successful import call/return parts are present in `error_parts`.
- [x] Add focused unit tests for extraction edge cases and DB rewriting behavior, including realtime event emission and preservation of followups/submission metadata.
- [x] Add or update runtime wiring coverage proving a run that logs a successful homework import mutates the original checklist message.
- [x] Document the behavior in the checklist/homework section of `AGENTS.md`.
- [x] Run `cd server && uv run ruff check . && uv run mypy src && uv run pytest` and fix failures.

## Acceptance Criteria & Unit Tests

- Given an assistant checklist containing item id `hwgeo` unchecked, a later successful run writing `{"hw_id": "hwgeo", "done": true}` to `school_homework_done` causes the stored checklist item to become `checked: true`.
- The reverse write `done: false` clears the checkbox.
- `submissions`, `submitted_by`, `submitted_at`, and `followups` are unchanged.
- All matching visible assistant checklist messages in the same conversation are updated.
- Nonmatching items, other conversations, hidden historical answer payloads, and non-checklist parts are unchanged.
- A realtime `message.updated` is emitted for each rewritten message.
- Failed/retrying imports, wrong tables, `mode="replace"`, empty imports, `inserted != len(rows)`, malformed returns, missing/nonboolean `done`, and missing/empty `hw_id` do not update checklists.
- Existing checklist submit behavior remains green.
- Server verification passes: `uv run ruff check .`, `uv run mypy src`, `uv run pytest`.

## Parallelism Notes

This is a single serialized implementation stage. It touches agent runtime and checklist code, so it must not run parallel with other agent-runtime/checklist work. No exclusive external resources are needed: tests use the local sqlite pytest infrastructure and no production DB, MCP analytics service, network service, migration, or port allocation. Deployment is explicitly out of scope for this plan.
