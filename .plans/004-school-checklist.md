---
id: "004"
title: "Stage 4: Deploy + school homework checklist + docs"
status: done
prev_plan: ["002-checklists-pwa.md", "003-checklists-telegram.md"]
next_plan: []
devin_session_id: boulder-script
resources: ["deploy.sh", "prod-db", "admin-skill-edit"]
touches:
  - AGENTS.md
  - deploy/ (deploy run only)
  - prod: skills row (school), analytics sandbox.school_homework_done
---

# Stage 4: Deploy + school homework checklist + docs

## Goal & Scope
Deploy S1–S3, then extend the ephemeral `school` skill so it can show homework as a checklist (`show_checklist`) and record "done" homework into a new append-only analytics table `sandbox.school_homework_done`. This stage writes to production (deploy, analytics table DDL, editing the `school` skill row) — each prod write needs the user's explicit OK.

## Design decisions
- "done" state lives in a separate append-only table `sandbox.school_homework_done(hw_id, date, subject, done, marked_by, marked_at)` — because `push.py` rebuilds `school_homework` with TRUNCATE+INSERT every refresh, so a `done` column there would be wiped. Newest row per `hw_id` wins.
- **Per user decision, the table is NOT pre-created:** the first `analytics_import_rows(mode="append")` call creates it with types guessed from values (`date`/`marked_at` become text). No upfront DDL step.
- `hw_id = left(md5(date||'|'||subject||'|'||text),10)` computed in SQL; the agent only copies it, never invents it.
- Agent writes via existing `analytics_import_rows(mode="append")`; reads via read-only `analytics_query`.

## Step-by-Step Checklist
- [x] `deploy/deploy.sh`, then verify `healthz` → `{"db":"ok","qdrant":"ok"}` (HTTP 200).
- [x] Read live `school` SKILL.md and prepare new text → `/tmp/school_skill_new.md` (adds «Показать домашку чек-листом», «Ответ чек-листа», «"сделал биологию" в тексте», triggers «отметь домашку» etc.). Additive diff only.
- [x] Apply the skill text — applied via SSH+psql (version 1→2, triggers 6→10, content verified byte-identical to `/tmp/school_skill_new.md`).
- [x] Check roles: `tools.allow` = `["*"]` for child/adult/speaker (covers `show_checklist`); `school` skill `role_ids=[]` (all roles); child has `analytics` in `mcp_servers`. No changes needed.
- [x] E2E data layer: SQL round-trip verified against real analytics DB (append `done` row → LATERAL join reads `done=true`; type inference `done`=boolean, `date`/`marked_at`=text; rolled back). Endpoint smoke: `POST /messages/{mid}/checklist` → 401 (route live).
- [ ] E2E UI (PWA + Telegram): «покажи домашку» → toggle → «Отправить» → rows in `school_homework_done` → re-ask shows saved state. Deferred to Stage 5 / manual (needs a logged-in session).
- [x] `AGENTS.md`: add «Чек-листы» section, update «Школьные данные».

## Acceptance Criteria
- [ ] E2E passes (PWA + Telegram): toggling homework writes rows; re-asking shows saved state.
- [ ] Docs updated.

## Risks
- Model may repeat the list in text despite tool note.
- `school_homework_done` columns (`date`/`marked_at`) are created as text (types guessed from the first append) — date filtering/sorting is lexicographic; the skill text should use a consistent ISO format so it still sorts.
- Teacher editing homework text changes `hw_id` → done mark lost for that item.
- Roles with explicit `tools.allow` need `show_checklist` added by hand.
