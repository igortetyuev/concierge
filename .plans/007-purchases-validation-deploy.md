---
id: "007"
title: "Stage 3: Purchase lists real-model validation, docs, optional deployment"
status: done
prev_plan: ["006-purchases-agent.md"]
next_plan: []
resources: ["production-ssh", "production-db", "systemd-service", "telegram-bot", "lan-tts-services"]
files:
  - AGENTS.md
  - server/src/concierge/purchases/default_skill.py
  - server/src/concierge/purchases/**
  - server/src/concierge/agent/**
  - server/tests/**
agent_ids:
  coder: 5d9311b0
  tester: e18fb676
---

# Stage 3: Purchase validation, documentation and optional deployment

## Goal & Scope

Validate the purchase-list feature end to end, tune the default skill only where real behavior shows prompt/extraction problems, document operational semantics in `AGENTS.md`, and optionally deploy after explicit user approval.

This stage is intentionally broad in `files` because real-model validation can expose defects anywhere in stages 005–006. Production-code fixes still belong to the responsible coder subagent, not the orchestrator.

## Environment prerequisites

- Stage 006 is `done` and all server unit checks pass.
- For local/server validation: project dependencies installed with `cd server && uv sync`.
- For real-model validation: configured `chat` capability binding and a way to run or access the concierge server.
- For Telegram validation: enabled bot, linked user and existing Telegram configuration.
- For voice validation: configured `stt`/`tts` bindings and LAN Speaches/Kokoro services if testing production-like voice behavior.
- For deployment only: `deploy/.env` configured with `CONCIERGE_REMOTE`, `CONCIERGE_DOMAIN`, `CONCIERGE_DEST`; SSH access; already provisioned PostgreSQL/systemd/Caddy host.

## Explicit production approval gate

Deployment and any production writes are not authorized by this plan alone. Before running `deploy/deploy.sh`, migrations on production, editing production rows, restarting services, or performing live production validation writes, the orchestrator must stop and obtain explicit user approval in the conversation.

User decision in this session: deployment is approved **only after** the workflow-level final release review and integration/E2E checks. Therefore local validation/documentation may close this stage while deployment remains a pending post-review release action. If final review or E2E fails, deployment must not proceed.

## Step-by-Step Checklist

- [x] Run the full server verification:
  - [x] `cd server && uv run ruff check .`
  - [x] `cd server && uv run mypy src`
  - [x] `cd server && uv run pytest`
- [x] Because frontend changes are not expected, decide whether web verification is needed; no web files changed, so `cd web && pnpm lint` and `cd web && pnpm build` were not required.
- [x] Validate real-model scenario matrix in an available test/dev environment:
  - [x] «надо купить 2 килограмма яблок, литр молока и три бутылки воды»
  - [x] «закончились яблоки»
  - [x] «купить не два, а один килограмм яблок»
  - [x] «сделай молока в полтора раза больше»
  - [x] «хлеб не надо»
  - [x] «запланируй на завтра купить зубную пасту»
  - [x] «что осталось купить сегодня»
  - [x] «покажи все списки»
  - [x] «я в пятерочке» → «что тут надо купить»
  - [x] «купил молоко за 89 рублей»
  - [x] «я ушел»
  - [x] «поделись списком с Настей и Тихоном»
  - [x] «Тихон должен купить килограмм яблок и литр молока в Пятерочке»
- [x] Validate channels where feasible:
  - [x] PWA markdown rendering;
  - [x] PWA checklist toggle/submission;
  - [x] Telegram checklist buttons;
  - [x] voice quick/session spoken output uses compact spoken rendering rather than raw markdown.
- [x] Tune `server/src/concierge/purchases/default_skill.py` only where validation reveals concrete extraction/tool-use failures.
- [x] Route any production-code defect fixes through `develop-coder`; test-only fixes through `develop-tester` where appropriate.
- [x] Update `AGENTS.md` with a concise «Списки покупок» section covering schema semantics, tools, sharing/assignment rules, store context, date semantics, spoken override, skill configuration and checklist MVP limitations.
- [x] Record deployment decision: approved only after workflow-level final release review and integration/E2E checks; execute as a post-review release action, not as a prerequisite for closing this validation stage.

Post-review deployment actions required before the final report claims production completion:

- run `deploy/deploy.sh`;
- verify `curl -k https://$CONCIERGE_DOMAIN/api/healthz` returns ok;
- verify alembic head includes the purchase migrations;
- verify shared skill `purchases` exists, is enabled and has expected visibility;
- run one manual end-to-end purchase scenario in production.

## Acceptance Criteria & Unit Tests

This stage has no new unit-test ownership beyond fixes; integration/E2E tests belong to the workflow-level Stage 5 release process.

Acceptance criteria:

- Full server checks are green after any tuning/fixes.
- Real model consistently calls purchase tools rather than pretending to maintain lists in prose.
- Visual list output and spoken output differ as intended and remain accurate.
- Documentation in `AGENTS.md` is sufficient for future maintenance.
- Production deployment is explicitly user-approved only as a post-release action after final review/E2E; this validation stage may close before deployment, and deployment failure or cancellation must be reported separately.

## Parallelism Notes

This is the terminal serialized stage. It can touch the same files as stages 005–006 for validation fixes, uses production resources if approved, and therefore must not run parallel with any other purchase work.
