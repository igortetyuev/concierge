---
id: "009"
title: "Stage 2: Deploy homework checklist sync"
status: done
prev_plan: ["008-homework-checklist-sync.md"]
next_plan: []
resources:
  - production-ssh
  - systemd-service
  - production-db
  - analytics-db
files:
  - deploy/
agent_ids:
  coder: 4c15ca7f
  tester: null
---

# Stage 2: Deploy homework checklist sync

## Goal & Scope

Deploy the completed homework-checklist synchronization server change to production and perform deployment validation. This stage starts only after `.plans/008-homework-checklist-sync.md` is done.

In scope:

- run the existing deployment script;
- verify service health after restart;
- perform lightweight production validation of the deployed behavior.

Out of scope:

- editing the production DB-stored `school` skill (the implementation must not require it);
- artificial append-only homework rows unless the user explicitly approves that concrete write at execution time;
- schema migrations or infrastructure changes beyond the normal deployment script.

## Step-by-Step Checklist

- [x] Confirm Stage 008 is done and the working tree contains the intended server/test/doc changes.
- [x] Read `deploy/.env` availability locally without printing secrets; verify required deployment variables exist.
- [x] Run `deploy/deploy.sh`.
- [x] Verify `curl -k https://$CONCIERGE_DOMAIN/api/healthz` returns healthy db/qdrant status.
- [x] Validate in production PWA or by the least invasive available means that a free-text homework completion command updates an already shown checklist without reload. Do not create artificial append-only rows without explicit user approval for that specific write.
- [x] Report deployment output, healthcheck result, and validation outcome.

## Acceptance Criteria & Unit Tests

- Deployment script completes successfully.
- `/api/healthz` is healthy after restart.
- Production validation confirms the checkbox state in an already-rendered homework checklist changes after a text command, or any blocker to safe validation is reported explicitly.
- No production `school` skill edit is performed.
- No artificial rows are written to `sandbox.school_homework_done` without explicit user approval for that write.

No unit tests belong to this stage; verification is deployment smoke/manual validation.

## Parallelism Notes

This stage uses exclusive production resources and must run serially after Stage 008. It must not overlap with any other deployment or production mutation.

## Production validation note

Deployment completed successfully on 2026-11-27 and `/api/healthz` returned `{"db":"ok","qdrant":"ok"}`. Behavioral validation initially required a real append-only homework row, so it was paused for user authorization. The user explicitly authorized a concrete test write and sending the test command as Igor. Validation then used conversation #72/message 984, where geography was stale at `checked=true` while the latest domain state was already `done=false`. The first literal command «не сделана география» was interpreted by the model as already-recorded and produced no write; a clarified command «запиши ещё раз, что география не сделана» caused exactly one approved duplicate-state append (`hw_id=a2dc667e54`, `done=false`, `marked_by=Игорь`, `marked_at=2026-11-27T13:59:05`). The stored checklist item then changed to `checked=false`, confirming end-to-end reconciliation on production. Temporary technical devices used for API access were revoked.
