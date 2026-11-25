---
id: speaker-voices
title: Распознавание говорящего (speaker-ID) — голоса, маппинг на пользователей, дообучение
planned: 2026-11-13
status: done
depends_on: []
conflicts_with: []
touches:
  - server/src/concierge/media/**
  - server/src/concierge/agent/service.py
  - server/src/concierge/agent/runtime.py
  - server/src/concierge/api/admin.py
  - server/src/concierge/api/conversations.py
  - server/src/concierge/channels/telegram/service.py
  - server/src/concierge/cli.py
  - server/alembic/versions/0011_*.py
  - server/tests/**
  - web/src/lib/api.ts
  - web/src/components/MessageParts.tsx
  - web/src/components/admin/AdminView.tsx
  - web/src/components/admin/AdminVoices.tsx
  - AGENTS.md
  - ../speakerid/**            # новый соседний проект ~/projects/speakerid
claimed_by: devin-desktop session (main @ 71b850d)
claimed_at: 2026-11-13T21:18:47+0200
finished_at: 2026-11-13T22:05:00+0200
blocked_reason: null
---

## Goal

Каждое аудио-вложение (голосовые из PWA/Telegram, hold-to-talk колонки
esp32home) помимо транскрипта получает voice-embedding. Система с первого же
сообщения разделяет голоса: незнакомый отпечаток создаёт новую сущность
`Voice` («Голос N»), знакомый — пополняет существующую. Админ на странице
«Голоса» в `/admin` видит все голоса, прослушивает образец, мапит голос на
пользователя системы, объединяет несколько голосов в один (в т.ч. один
человек с разных микрофонов), удаляет ошибочные сэмплы — и система
дообучается на каждом новом распознанном клипе. Атрибуция прокидывается в
ленту (chip на сообщении) и в промпт агента (имя говорящего вместо имени
аккаунта-отправителя). Права не меняются: всё, сказанное через колонку,
исполняется с правами сервисного аккаунта колонки.

## Relations

- В реестре только done-план `concierge-roadmap` — формальных зависимостей нет.
- **Незакоммиченная работа (per `git status`):** `MessageParts.tsx`,
  `agent/prompt.py`, `test_agent.py`, `ForwardDialog.tsx` — доработка
  forwards. `MessageParts.tsx` входит в `touches` этого плана: исполнитель
  должен мёржить изменения аккуратно, не откатывая чужие правки. Worktree
  `/private/tmp/concierge-head` (detached HEAD) — старый, не ветка работы.
- Соседний проект `~/projects/mcp_livingroom` — образец оформления спутника
  (uv+hatchling, FastMCP→здесь FastAPI, README с регистрацией). Не зависимость.

## Context

**Пайплайн аудио (точка встраивания):**
`server/src/concierge/media/pipeline.py:117-128` — ветка `audio`: blob →
`_to_wav` (моно 16 kHz, ровно то, что нужно ECAPA-TDNN) →
`stt.transcribe_audio` → `derived = {transcript, duration_s, status}`.
Ветка `video` (`:134-139`) так же получает wav-дорожку. Результат пишется в
`attachments.derived` (JSONB, `media/models.py`), событие
`attachment.updated` — в `_publish` (`:154`).

**Capability-инфраструктура:** `agent/service.py:6-15` — кортеж
`CAPABILITIES`; `get_capability_binding(session, cap, role_id)` читает
`capability_bindings` при каждом вызове. Провайдер — строка
`model_providers` (`agent/models.py:13-35`): `kind`, `base_url`,
`api_key_ref`. Разбор по kind'ам — внутри потребителя (см. `media/stt.py`:
`test`, `deepgram`, `openai-compatible`; `http_base_url`,
`provider_api_key` — `agent/providers.py:84-102`). Для `speaker` вводим
kind `speaker-http` (POST multipart wav → `{embedding: [...]}`) и `test`.

**Где рождаются transcribed-парты** (туда копируем `speaker` из
`att.derived` после `media_service.transcribe`):
- `api/conversations.py` `voice_quick` — `:667-671`
  (`{"type":"text","text":transcript,"transcribed":True}` + audio-парта);
- `api/conversations.py` `voice_message` — `:767-775` (text-парта с
  `voice_attachment_id`, аудио встроено в неё, отдельной audio-парты нет);
- `channels/telegram/service.py:605-619` — личные voice: text-парта
  `transcribed` + audio-парта.

**Отдача наружу:** `media/service.py` `attachment_to_dict` (`:165-182`) и
`expand_part` (`:206-216`) — белые списки ключей `derived`; добавить
`"speaker"`. `expand_part` раскрывает только парты с `attachment_id` —
поэтому для text-парт (`voice_attachment_id`) `speaker` копируется в саму
парту при создании сообщения.

**Промпт агента:** `agent/runtime.py` `_content` (`:570-603`) — собирает
текст сообщения; для group концов `text = f"{member_names.get(author_id)}:
{text}"`. Здесь подставляем имя голоса вместо имени аккаунта (колонки).
`media_prompt_text` (`media/service.py:194`) — как attachment читается
агенту; транскрипт дублируется: text-парта уже даёт текст, audio-парта
снова отдаёт `media_prompt_text` — поведение сохранить, менять только
префикс автора.

**Админка:** `api/admin.py` — роутер `prefix="/admin"`, депсы
`ManageUsers = Depends(require("manage_users"))` (`:36`). Табы:
`web/src/components/admin/AdminView.tsx` — `TABS` (`:26-35`), perm
`manage_users`. Модели — SQLAlchemy `Base` из `core/db.py`, JSONBType-
паттерн см. `media/models.py`. Миграции — `server/alembic/versions/`
(последняя `0010_deleted_status.py`). CLI — `server/src/concierge/cli.py`
(argparse-подкоманды, `bootstrap-admin` — образец).

**Инфраструктура инференса:** whisper/Kokoro — Speaches 0.8.3 на Windows-
хосте (`http://<win-host>:8000/v1`, venv + Run-ключ `Speaches`, firewall-
правило; `HF_HUB_OFFLINE=1` в `run-speaches.ps1` — см. AGENTS.md «STT/TTS»).
Box — Celeron N4100, 3.7 GiB RAM: тяжёлый инференс туда нельзя.

**Тесты:** `server/tests/test_voice_message.py`, `test_media.py`,
`test_admin.py`; SQLite + `test`-провайдер (см. kind `test` в
`stt.py:36-39`).

## Decisions

- **Хост инференса: Windows-хост, рядом со Speaches** (выбор пользователя).
  Новый соседний проект `~/projects/speakerid` (по образцу
  `mcp_livingroom`): FastAPI + SpeechBrain ECAPA-TDNN
  (`speechbrain/spkrec-ecapa-voxceleb`, 192-dim) на torch CPU. Порт 8100.
- **Только атрибуция, права — у отправителя** (выбор пользователя, уточнено:
  «все распознанное колонкой исполнять с правами колонки»). Голос влияет на
  подпись в ленте/промпте и на то, как агент адресуется; роль, права,
  память и runs остаются за аккаунтом-отправителем.
- **Кластеризация с первого сообщения:** нет «режима обучения» — любой
  эмбеддинг ниже `match_threshold` создаёт новый `Voice`; выше — попадает в
  ближайший. Это покрывает «несколько голосов одного человека» (разные
  микрофоны → разные кластеры → merge в админке) и чужие/гостевые голоса.
- **Дообучение двумя путями:** авто — сэмпл дописывается в `voice_samples`
  и центроид пересчитывается при `score >= learn_threshold` (защита от
  отравления центроида плохими матчами); ручное — админ добавляет
  attachment к голосу / переносит и удаляет сэмплы / мёржит голоса.
- **Сэмплы хранятся, центроид пересчитывается** — иначе merge/удаление
  сэмплов невозможно откатить. `voice_samples.attachment_id` UNIQUE:
  повторный прогон пайплайна (retranscribe) обновляет строку, а не плодит
  дубликаты. На голос — не более 300 последних сэмплов (старые чистятся).
- **Speaker-данные — в `derived.speaker`** `{"voice_id","user_id","name",
  "score"}` и копия в text-парте сообщения; не трогаем текст транскрипта.
- **Пороги — в params биндинга** (`match_threshold` 0.45, `learn_threshold`
  0.60, `min_duration_s` 1.0 — стартовые, крутить на реальных записях;
  при `< min_duration_s` эмбеддинг не считается).
- **Отказоустойчивость:** без биндинга `speaker` или при ошибке сервиса —
  warning, `derived.speaker` отсутствует, транскрипт не страдает. Никогда
  не роняем пайплайн из-за speaker-ID.
- **Backfill** — CLI-подкоманда `backfill-speaker` (опционально, не
  автоматом): прогон по существующим audio-attachments.

## Steps

1. **Сайдкар `~/projects/speakerid`** (новый проект, как `mcp_livingroom`):
   uv + hatchling, `fastapi`, `uvicorn`, `speechbrain`, `torch`,
   `torchaudio`, `soundfile`. `src/speakerid/app.py`: при старте
   `EncoderClassifier.from_hparams("speechbrain/spkrec-ecapa-voxceleb",
   run_opts={"device":"cpu"})`; `POST /embed` (multipart `file` →
   нормализация до 16kHz-моно через torchaudio resample при необходимости →
   `encode_batch` → L2-нормированный вектор) → `{"embedding": [...], "dim":
   192}`; `GET /healthz` → `{"ok": true, "dim": 192}`. Env: `SPEAKERID_PORT`
   (8100), опционально `HF_HUB_OFFLINE`. `tests/test_app.py` — синтетический
   тон → shape/norm эмбеддинга, детерминизм. README: venv на Windows-хосте,
   `pip install .`, первый прогон без `HF_HUB_OFFLINE` (скачать модель в
   кэш), Run-ключ `SpeakerId` →
   `.../uvicorn speakerid.app:app --host 0.0.0.0 --port 8100`, firewall-
   правило 8100.
   **Verify:** `cd ~/projects/speakerid && uv sync && uv run pytest`;
   `uv run uvicorn speakerid.app:app --port 8100` +
   `curl localhost:8100/healthz`, `curl -F file=@x.wav /embed` → 192 числа.

2. **Миграция `0011_voices.py` + модели** в
   `server/src/concierge/media/models.py`:
   `voices(id, label String(64), user_id FK users.id NULL, centroid JSONB,
   sample_count int, sample_attachment_id FK attachments.id NULL,
   last_heard_at, created_at)`; `voice_samples(id, voice_id FK voices.id
   CASCADE, attachment_id FK attachments.id UNIQUE, embedding JSONB,
   score Float, created_at)`, индекс по `voice_id`.
   **Verify:** `cd server && uv run alembic upgrade head` (dev) +
   `uv run alembic downgrade -1 && uv run alembic upgrade head`.

3. **`server/src/concierge/media/speaker.py`** — новый модуль:
   `embed_audio(session, user, wav_path) -> list[float] | None` (биндинг
   `speaker`, kind `test` → `params["embedding"]`, kind `speaker-http` →
   `POST {base}/embed` multipart, Bearer из `provider_api_key` если задан;
   ошибки → warning + None); `cosine(a,b)`; `assign_voice(session, att,
   vec, params)` → матч по центроидам, создание Voice «Голос N»
   (N = max(id)+1), upsert `voice_samples` по `attachment_id`, пересчёт
   центроида при learn_threshold/первом сэмпле, кап 300 сэмплов, возврат
   dict для `derived["speaker"]`; `recompute_centroid(session, voice_id)` —
   нормированное среднее сэмплов. В `agent/service.py:CAPABILITIES`
   добавить `"speaker"`.
   **Verify:** новый `server/tests/test_speaker.py` — sqlite-юниты:
   первый эмбеддинг → новый Voice; близкий → тот же голос и центроид
   сдвинулся; далёкий → второй голос; retranscribe-прогон → сэмпл один
   (UNIQUE-upsert). `uv run pytest tests/test_speaker.py`.

4. **Пайплайн:** в `media/pipeline.py` ветка `audio` — после `_to_wav`
   параллельно (`asyncio.gather`) `stt.transcribe_audio` и
   `speaker.embed_audio`; при наличии вектора и `duration >=
   min_duration_s` → `speaker.assign_voice` → ключ `"speaker"` в
   возвращаемый dict; исключения speaker-ветки ловить отдельно (warning,
   без искажения результата STT). Тот же вызов эмбеддинга для аудиодорожки
   видео (`:134-139`), `out["speaker"]`.
   **Verify:** `test_voice_message.py`/`test_media.py` — с test-провайдером
   `speaker` (params.embedding) `att.derived["speaker"]["voice_id"]`
   выставлен; без биндинга — ключа нет, статус `done`.

5. **Прокидка наружу:** `media/service.py` — `"speaker"` в белые списки
   `attachment_to_dict` (`:179`) и `expand_part` (`:213`). В
   `api/conversations.py` `voice_quick` и `voice_message` и в
   `channels/telegram/service.py` — после `transcribe`/`refresh` копировать
   `att.derived.get("speaker")` в text-парту как `"speaker"`.
   **Verify:** тест `test_voice_message` — text-парта содержит
   `speaker.name`/`voice_id`; расширенная парта audio — тоже.

6. **Промпт агента:** `agent/runtime.py` `_content` — собрать
   `speaker_label` из `speaker`-полей партов или `att.derived["speaker"]`
   аудио-частей; для group: префикс = display_name юзера или label голоса
   (незнакомый — как есть, «Голос N»), подставляется вместо
   `member_names.get(author_id)`. Формат — тот же `f"{label}: {text}"`.
   **Verify:** юнит на `_content` (или сквозной test_agent): сообщение от
   колонки с mapped voice → в prompt text фигурирует имя юзера, не
   «esp32home».

7. **REST голосов** в `api/admin.py` (депс `ManageUsers`):
   `GET /voices` (список + user display_name + sample_count +
   last_heard_at + sample_attachment_id), `PATCH /voices/{id}`
   (`{label?, user_id?}` — null снимает маппинг), `POST /voices/{id}/merge`
   (`{into_id}` → перепривязать сэмплы, `recompute_centroid`, удалить
   источник), `DELETE /voices/{id}`, `GET /voices/{id}/samples`,
   `POST /voices/{id}/samples` (`{attachment_id}` → reuse/пересчёт
   embedding'а и перенос сэмпла), `DELETE /voices/{id}/samples/{sid}` →
   recompute. Аудит через `core/audit` как соседние ручки.
   **Verify:** `tests/test_voices.py` — CRUD + merge переносит сэмплы и
   пересчитывает центроид; `uv run pytest tests/test_voices.py`.

8. **Веб:** `web/src/lib/api.ts` — функции эндпоинтов голосов;
   `web/src/components/admin/AdminVoices.tsx` — список: label (редакт.),
   select пользователя (`GET /admin/users`), кнопка проигрывания
   `sample_attachment_id` (`<audio src="/api/attachments/{id}">`), счётчик
   сэмплов, last_heard; действия: merge (диалог выбора целевого голоса),
   удалить; раскрываемый список сэмплов (play + удалить). В `AdminView.tsx`
   — таб `{id:'voices', label:'Голоса', perm:'manage_users'}`. В
   `MessageParts.tsx` — маленький chip с `speaker.name` на
   transcribed/audio-партах (не ломая идущие правки forwards — файл в
   рабочем дереве изменён!).
   **Verify:** `cd web && pnpm lint && pnpm build`.

9. **CLI `backfill-speaker`** в `server/src/concierge/cli.py`:
   `--limit N --conversation-id ID` — по audio-attachments без
   `derived.speaker`: прогнать embed+assign (внутренний вызов функций из
   шага 3/4, не HTTP). Для ре-проходов безопасен за счёт UNIQUE-upsert.
   **Verify:** `uv run concierge backfill-speaker --limit 5` на dev-базе —
   в таблице `voices` появились кластеры, `derived.speaker` заполнен.

10. **Документация и конфигурация:** AGENTS.md — новый раздел «Speaker-ID»
    (архитектура, таблицы, развёртывание сайдкара на Windows-хосте по
    образцу Speaches: venv, Run-ключ `SpeakerId`, firewall 8100; провайдер
    `speaker-lan` kind `speaker-http` base_url `http://<win-host>:8100`;
    биндинг `speaker` с params-порогами; админ-страница «Голоса»).
    Установить провайдер и биндинг в `/admin` после деплоя сайдкара.
    **Verify:** ручной прогон — голос с колонки создаёт «Голос N»,
    маппинг на пользователя меняет подпись в ленте и в prompt'е,
    merge двух голосов работает; `cd server && uv run ruff check . &&
    uv run mypy src && uv run pytest`; `cd web && pnpm lint && pnpm build`.

## Out of scope

- Диаризация нескольких говорящих внутри одного клипа (один клип = один
  голос); pyannote-диаризация — отдельная задача.
- Использование распознанного голоса для прав, ролей, памяти или
  аутентификации — осознанно отвергнуто (подделываемо).
- Автоматический маппинг голоса на пользователя без рук админа.
- Очистка `derived.speaker` на attachments при удалении голоса (stale-ссылка
  безвредна — UI показывает label из `derived`, не join'ит).
- Переписывание уже сохранённых сообщений при пере-маппинге голоса —
  `speaker` в старых партах остаётся как было на момент отправки (join только
  в `derived`; text-парты — слепок).

## Open risks

- **Качество на esp32-микрофоне** — главная неизвестная: дальнее поле, шум
  ТВ/саундбара, короткие фразы. Стартовые пороги (0.45/0.60) могут дать
  лишние кластеры или склейки — планируемая калибровка: накопить
  backfill'ом, подкрутить `match_threshold`/`learn_threshold` в params
  биндинга без правок кода. Если ECAPA окажется слабой на этом канале —
  заменить модель в сайдкаре (WeSpeaker/CAM++), контракт `/embed` не меняется.
- **Незакоммиченные правки** в `MessageParts.tsx`/`agent/prompt.py` (forwards)
  — конфликт мёржа на шаге 8. Исполнитель: сохранить чужие изменения,
  делать точечный diff; при расхождении — остановиться и спросить.
- **Первый запуск сайдкара** на Windows ходит на huggingface.co за моделью
  (~80 МБ); после кэша включать `HF_HUB_OFFLINE=1` как у Speaches, иначе
  при отвалившейся связи запросы упадут.
- **torch на Windows-хосте** — тяжёлая зависимость (~200+ МБ), впервые в том
  окружении (Speaches работает на ctranslate2/ONNX). Если окружение не
  соберётся — fallback: ONNX-экспорт ECAPA + onnxruntime (есть готовые
  экспорты в SpeechBrain/WeSpeaker); меняется только сайдкар.
- **Отравление центроида** серией плохих авто-матчей — смягчено
  learn_threshold и ручным удалением сэмплов; при симптомах «голос
  расползается» чистить сэмплы в админке.
- **Колонка в «Гостиной» post'ит не только голос** — проверить, что
  speaker-ветка не тормозит пайплайн остальных типов (embed только в
  audio/video-ветках).

## How to execute

---
**Execute this plan in a fresh context**

Start a clean session — `/clear` in this terminal, or `claude` in a new one — then paste:

```
Implement the plan at .claude/plans/2026-11-13-speaker-voices.md.
Pre-flight, before any change:
1. Read the plan. For every id in depends_on, read .claude/plans/*<id>.md: its status must be `done`. If any is not, stop and tell me which ones and their status.
2. For every other plan with status `in_progress`: if its id is in conflicts_with, or its `touches` overlap ours, stop and tell me which plan and which files.
3. Run `git status --short`: if there are uncommitted changes in our `touches`, stop and tell me.
4. Claim the plan: set status: in_progress, claimed_by: <this session / branch>, claimed_at: <now> in its header.
Then work the steps in order and run each step's Verify check before moving on.
If a step turns out to be wrong, blocked, or ambiguous, set status: blocked with blocked_reason, stop, and tell me instead of improvising.
After the last Verify passes, set status: done and finished_at: <now>.
```

The plan is self-contained: it carries its own context, decisions, ordering, and verification checks, so the executing session needs nothing from this conversation.
---
