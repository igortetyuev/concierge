---
id: "003"
title: "Stage 3: Telegram checklist buttons"
status: done
prev_plan: ["001-checklists-server.md"]
next_plan: ["004-school-checklist.md"]
devin_session_id: peat-swoop
resources: []
touches:
  - server/src/concierge/channels/telegram/sink.py
  - server/src/concierge/channels/telegram/service.py
  - server/src/concierge/channels/telegram/gateway.py
  - server/tests/test_telegram.py
---

# Stage 3: Telegram checklist buttons

## Goal & Scope
Render a `checklist` part in Telegram as a numbered listing with one `☐/☑ N. short label` button per item plus a «Отправить» button, and handle the toggle/submit callbacks. Callback data: `cl:{mid}:{idx}:{maskhex}` toggles one item, `cs:{mid}:{maskhex}` submits (bit i = item i checked; ≤ ~30 bytes, under the 64-byte limit).

## Step-by-Step Checklist
- [x] `sink.py`: new `_send_checklist` called after `_send_choice` (~:198) — numbered full listing (MarkdownV2, fallback plain text) + per-item buttons (≤40 chars) + «Отправить» row.
- [x] `service.py`: `TelegramOutbox` (~:95-141) gets `edit_markup(chat_id, message_id, reply_markup)`; `handle_callback` (~:268) gets `message_id` param, handles `cl:` (flip bit + `edit_markup`) and `cs:` (`submit_checklist(..., "telegram")` + «Отправлено»).
- [x] `gateway.py`: `AiogramOutbox.edit_markup` → `edit_message_reply_markup`; `_on_callback` (~:263-270) passes `cb.message.message_id`.
- [x] Tests in `tests/test_telegram.py`: `FakeOutbox` (~:46) gets `edit_markup`; cover keyboard layout + callback format, toggle edits keyboard, submit creates answer + run, refuse non-members.

## Acceptance Criteria & Unit Tests
- [x] `cd server && uv run ruff check . && uv run mypy src && uv run pytest tests/test_telegram.py` — green.

## Out of scope
Updating the Telegram keyboard after a submit from the PWA (cross-channel sync). PWA widget (S2), prod deploy & `school` skill (S4).
