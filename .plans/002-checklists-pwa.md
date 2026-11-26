---
id: "002"
title: "Stage 2: PWA checklist widget (ChecklistPart)"
status: done
prev_plan: ["001-checklists-server.md"]
next_plan: ["004-school-checklist.md"]
devin_session_id: amplified-pharaoh
resources: ["vite-port-5173"]
touches:
  - web/src/components/ChecklistPart.tsx (new)
  - web/src/components/MessageParts.tsx
  - web/src/lib/api.ts
  - web/src/index.css
---

# Stage 2: PWA checklist widget

## Goal & Scope
Render a `checklist` part in the PWA as interactive checkboxes with an «Отправить» button, and wire the submit to `POST /conversations/{cid}/messages/{mid}/checklist`. List mode → `<ul>` of checkbox rows; table mode → `<table>` with the checkbox as first column and cells rendered via Markdown (so homework material links work).

## Step-by-Step Checklist
- [x] New `web/src/components/ChecklistPart.tsx`: local state starts from `items[].checked`, resets when stored state changes (`submissions`/checked ids); first submit always allowed, after that only when changed; after submit call `applyMessageCreated(reply)` + muted note with submit count.
- [x] `MessageParts.tsx`: add `checklist` case next to `choice` (~:730-739).
- [x] `lib/api.ts`: add `submitChecklist(...)` next to `answerChoice` (~:844).
- [x] `index.css`: `.checklist-*` styles next to `.choice-card` (~:2100-2170).

## Acceptance Criteria & Unit Tests
- [x] `cd web && pnpm lint && pnpm build` — green.
- [ ] Manual: checklist renders with checkmarks + «Отправить»; toggling + submit posts correct state (needs a running backend — local PG/Qdrant, or `VITE_API_TARGET` at the deployed server; otherwise deferred to S4 E2E). Deferred — no local backend was running during S2.

## Out of scope
Telegram UI (S3), prod deploy & `school` skill (S4).
