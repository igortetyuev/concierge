
---
id: scheduled-events
title: События по таймеру и напоминания (планировщик)
planned: 2026-11-18
status: done
depends_on: []
conflicts_with: []
touches:
  - server/pyproject.toml
  - server/uv.lock
  - server/alembic/versions/0013_scheduled_jobs.py
  - server/src/concierge/schedule/**
  - server/src/concierge/core/models.py
  - server/src/concierge/agent/runtime.py
  - server/src/concierge/agent/tools.py
  - server/src/concierge/api/app.py
  - server/src/concierge/api/schedule.py
  - server/src/concierge/channels/telegram/sink.py
  - server/tests/test_schedule.py
  - web/src/lib/api.ts
  - web/src/components/SettingsView.tsx
  - web/src/components/ChatView.tsx
  - web/src/components/MessageParts.tsx
  - AGENTS.md
claimed_by: devin-desktop-session
claimed_at: 2026-11-18
finished_at: 2026-11-18
blocked_reason: null
---
# События по таймеру и напоминания (планировщик)

Добавить в сервер персистентный планировщик (таблица scheduled_jobs + фоновый воркер) с однократными и повторяющимися паттернами (минуты…годы, дни недели, времена, даты/исключения), которые от имени пользователя запускают ран агента — либо командой в новой/переиспользуемой сессии, либо напоминанием в исходной сессии; управление через инструменты агента и экран «Расписание» в PWA.

---

## Goal

Пользователь в любой сессии может сказать консьержу «каждый будний день в 8:00 пришли сводку погоды», «напомни через 5 минут выключить плиту», «напоминай 1-го числа каждого месяца оплатить интернет, кроме января», «приостанови до 1 декабря», «отмени». Сервер хранит паттерны в `scheduled_jobs`, фоновый воркер в момент срабатывания постит триггер-сообщение от имени владельца паттерна и запускает обычный ран агента с ролью/правами владельца. Команды (`kind=command`) с периодом ≥ суток получают новую сессию на каждое срабатывание; более частые переиспользуют сессию паттерна (новая — при первом срабатывании или если прежняя архивна/удалена). Напоминания (`kind=reminder`) срабатывают в сессии, где их попросили (в т.ч. в сессиях, созданных планировщиком): архивная возвращается в active, удалённая заменяется новой личной сессией. Отмена — жёсткое удаление строки; пауза — `paused_until`, срабатывания внутри паузы пропускаются. В PWA, в «Настройках», есть секция «Расписание»: список своих паттернов, ближайшее срабатывание, пауза до даты, возобновление, удаление.

## Relations

- `concierge-roadmap` (done), `speaker-voices` (done) — завершены; пересечение по `agent/runtime.py`/`MessageParts.tsx` только историческое. Формальных связей нет.
- Незакоммиченных изменений и параллельных веток нет (`git status` чист, HEAD `501ebdf Timezone/week of day fix`). `.claude/plans/drafts/` отсутствует — параллельное планирование не видно.

## Context

- **Таймзона**: `concierge/agent/prompt.py` `local_now()` (ZoneInfo из `Settings.timezone`, дефолт `Europe/Kaliningrad`, `core/config.py`), промпт уже содержит строку `[Сейчас] …` — агент знает локальное время. ORM-колонки — naive UTC (`core/db.py:30 utcnow`).
- **Ран агента**: `agent/runtime.py:80 schedule()` → `maybe_run()` (`:92`) — если lock сессии занят, ран **молча пропускается** (`:96`). `_run()` (`:104`) берёт пользователя из `trigger.author_id` (нужен user-authored триггер), выходит при `conv.status != "active"`, в `reply_mode == "mention"` требует `prompt.is_mentioned`. `_content()` (`:571`) рендерит парты в текст промпта; `_ensure_title` (`:728`) → `title.generate` ничего не делает, если title уже задан (`agent/title.py:36`). `_message_dict` (`:743`) — WS-шейп сообщения.
- **Сообщения/сессии**: `conversations/service.py` — `create_conversation(session, owner, kind, title, member_ids, origin)` (`:20`), `post_message(..., client_msg_id, source_channel)` идемпотентна по `(conv, author, client_msg_id)` (`:388`) и выставляет автору `last_read_seq`, `update_conversation(status=…)` ведёт `archived_at` (`:132`). Статусы `active|archived|deleted`. Паттерн «наследника линии» — `resolve_voice_conversation` (`:70`).
- **Фан-аут**: `channels/hub.py` — `publish` (подписчики сессии), `publish_users` (новые сессии: «Nobody is subscribed to a brand-new conversation yet», `api/conversations.py:201`). Событие `conversation.updated` подхватывает `web/src/store/realtime.ts:142`.
- **Фоновые воркеры**: образец — `memory/indexer.py` (`start_worker`/`stop_worker`/`kick` + `asyncio.wait_for(_kick.wait(), timeout)`), запуск в `api/app.py:57 lifespan`. Uvicorn — один процесс (`deploy/concierge.service`), hub in-memory — in-process воркер корректен.
- **Инструменты**: `agent/tools.py` — `ToolDeps(user_id, conversation_id, run_id, trigger_seq)` (`:39`), реестр `CORE_TOOLS` (`:668`), гейтинг `allowed_tool_names` по `tools.allow/deny` роли (`:697`; adult/child — `allow:["*"]`, guest — deny `*`). `NAMED_MODEL_TOOLS = tuple(CORE_TOOLS) + …` — новые инструменты автоматически попадут в чеклист именованных моделей (допустимо).
- **Telegram**: `channels/telegram/sink.py:154 _message_created` зеркалит user/system-сообщения в чат (`_mirror` с префиксом имени), anti-echo только для `source_channel == "telegram"`.
- **Модели/миграции**: последняя миграция `0012` (`alembic/versions/0012_voice_authenticated.py` — стиль), реестр моделей `core/models.py`, JSONB-тип `conversations/models.py:11 JSONBType`.
- **PWA**: `SettingsView.tsx` (секции `details-section`, образец `TelegramSection`), `ChatView.tsx:420` рендер `author_kind === 'system'` как `msg sys`/`sys-note`, `lib/api.ts` — `apiClient`.
- **Тесты**: `tests/conftest.py` (sqlite+aiosqlite, `ROLE_SEEDS`), `tests/test_agent.py` — TestModel/FunctionModel для ранов.
- `python-dateutil 2.9.0.post0` уже есть в `uv.lock` транзитивно.

## Decisions

1. **Напоминание = ран агента** (ответ пользователя): при срабатывании агент получает служебный триггер и сам формулирует напоминание (может учесть контекст сессии и инструменты). Отвергнуто: статичный текст без LLM.
2. **Единый механизм триггера** для обоих видов: user-authored сообщение от владельца паттерна, `source_channel="schedule"`, парт `{"type":"text","text":<job.text>,"scheduled":{"job_id","kind","title","occurrence"}}`, `client_msg_id = f"sched:{job_id}:{occurrence_iso}"` (идемпотентность). Так ран идёт с ролью/правами владельца без изменений в RBAC.
3. **Сессия для `command`**: `session_mode` вычисляется при создании: `new`, если однократное или минимальный интервал между соседними срабатываниями (по первым 50 будущим) ≥ 24 ч; иначе `reuse`. `new` → каждое срабатывание создаёт личную сессию владельца (`title=job.title`, `origin="schedule"`, `settings.schedule_job_id`). `reuse` → `job.conversation_id`, если он active и владелец в участниках; иначе новая (archived **не** восстанавливается — так в требовании). `job.conversation_id` обновляется на последнюю созданную.
4. **Сессия для `reminder`** (ответ пользователя): цель — `job.conversation_id` (сессия, где попросили, = `ctx.deps.conversation_id`). active → туда; archived → `update_conversation(status="active")` + `conversation.updated`; deleted/нет строки/владелец больше не участник → новая личная сессия владельца, `job.conversation_id` переключается на неё.
5. **Без внешних уведомлений** (ответ пользователя): только запись в сессию + WS. Telegram-sink не зеркалит триггер-сообщения `source_channel="schedule"` (в TG-привязанную сессию уходит только ответ агента).
6. **UI** (ответ пользователя): создание — только через агента; в PWA секция «Расписание» в `/settings` — список, пауза до даты, возобновить, удалить. REST только `GET/PATCH/DELETE`.
7. **Спецификация расписания** — структурированная модель `ScheduleSpec` (не сырой RRULE-строкой: проще валидировать и показывать в UI), исполняется через `dateutil.rrule`/`rruleset` в локальной таймзоне семьи. `python-dateutil` становится прямой зависимостью (+ `types-python-dateutil` в dev для mypy strict) — уже в lock транзитивно.
8. **Пропущенные срабатывания** (сервер лежал): коалесцируются — максимум одно срабатывание на паттерн при опоздании ≤ `MISSED_GRACE = 12 ч`, старше — пропуск с warning; затем `next_run_at` = следующее будущее. Claim at-most-once: `next_run_at` сдвигается и коммитится **до** запуска рана.
9. **Отмена** = `DELETE` строки. Исчерпанные паттерны (однократные после срабатывания, `until`/`count`) тоже удаляются после срабатывания.
10. **Занятая сессия**: для триггеров планировщика ран ждёт lock, а не пропускается (`maybe_run(..., wait=True)`).
11. **Лимит**: `MAX_JOBS_PER_USER = 100`; `interval ≥ 1`. Управлять можно только своими паттернами (админ — тоже только своими; админ-обзор вне скоупа).
12. **Отображение в чате**: триггер-сообщение рендерится не пузырём пользователя, а системной строкой `⏰ «title»: text` (стиль `msg sys`).

## Steps

1. **Зависимость и модель данных.**
   - `server/pyproject.toml`: `uv add 'python-dateutil>=2.9,<3'`, `uv add --dev types-python-dateutil` (версия, опубликованная ≥ 7 дней назад).
   - `server/src/concierge/schedule/__init__.py`, `schedule/models.py`: `ScheduledJob` (`__tablename__ = "scheduled_jobs"`): `id`, `user_id` FK users CASCADE (index), `kind` String(16) `command|reminder`, `title` String(256), `text` Text, `spec` JSONBType, `session_mode` String(8) `new|reuse`, `conversation_id` FK conversations SET NULL nullable, `next_run_at` datetime (index), `paused_until` nullable, `last_run_at` nullable, `fire_count` int default 0, `created_at` server_default now.
   - Зарегистрировать в `core/models.py`; миграция `alembic/versions/0013_scheduled_jobs.py` (down_revision `0012`, стиль 0012).
   - **Verify:** `cd server && uv run alembic upgrade head` на пустой sqlite/локальной БД проходит; `uv run mypy src` чисто.

2. **Рекуррентность — `schedule/recurrence.py`** (чистые функции, без БД).
   - `ScheduleSpec(BaseModel)`: `at: datetime | None` (однократно, локальное), `delay_minutes: int | None` (относительное «через N минут»; конвертируется в `at` при создании), `freq: Literal["minutely","hourly","daily","weekly","monthly","yearly"] | None`, `interval: int = 1` (≥1), `weekdays: list[Literal["mo","tu","we","th","fr","sa","su"]] | None`, `times: list[str] | None` (`"HH:MM"`, для daily+; по умолчанию время `start`), `month_days: list[int] | None` (1..31, −1 = последний), `start: datetime | None`, `until: datetime | None`, `count: int | None`, `dates: list[datetime] | None` (доп. разовые срабатывания; можно без `freq` — «по определённым датам»), `exclude_dates: list[date] | None` (исключить все срабатывания в эти локальные дни). Валидатор: ровно один источник из {`at`/`delay_minutes`} или хотя бы одно из {`freq`, `dates`}; naive datetime трактуются как локальные.
   - `build(spec, tz) -> rruleset`: по одному `rrule` на каждый элемент `times` (иначе byhour×byminute дал бы декартово произведение), `byweekday`, `bymonthday`, `rdate` для `at`/`dates`.
   - `next_after(spec, tz, after: datetime, paused_until: datetime | None) -> datetime | None` (UTC naive): `rs.after(max(after, paused_until))`, пропуская `exclude_dates`.
   - `is_daily_or_rarer(spec, tz, now) -> bool` (Decision 3), `upcoming(spec, tz, now, n=3)`, `describe(spec) -> str` (короткое русское описание для UI/инструментов: «каждые 2 ч», «по пн, ср в 08:00», «1-го числа ежемесячно», «однократно 19.11 09:00»).
   - **Verify:** unit-тесты в `tests/test_schedule.py`: once/delay, minutely interval=15, hourly, weekly weekdays+2 times, monthly month_days включая −1, yearly, dates-only, exclude_dates, until/count → `None` после исчерпания, paused_until пропускает срабатывания, классификация daily_or_rarer (daily 1 time → True; daily 2 times → False; hourly → False; once → True). `uv run pytest tests/test_schedule.py -k recurrence`.

3. **Сервис — `schedule/service.py`** (все функции принимают `now: datetime` для тестируемости).
   - `create_job(session, user, kind, title, text, spec, conversation_id, now) -> ScheduledJob` — нормализует `delay_minutes`→`at`, считает `session_mode`, `next_run_at` (ошибка, если `None` — «нет будущих срабатываний»), лимит `MAX_JOBS_PER_USER`; `reminder` сохраняет `conversation_id`, `command` — `None`. После коммита `worker.kick()`.
   - `list_jobs(session, user_id)`, `get_own_job(session, user_id, job_id)`, `pause_job(job, until, now)` (пересчёт `next_run_at` от `until`), `resume_job(job, now)`, `cancel_job(job)` (delete) — все с `kick()`.
   - `resolve_target(session, job, owner) -> tuple[Conversation, bool created]` — Decisions 3–4; новая сессия: `create_conversation(owner, "personal", job.title, [], origin="schedule")` + `settings={"schedule_job_id": job.id}`.
   - `fire(job_id, occurrence, now)`: в своей сессии БД — владелец (`is_active`, иначе только перенос `next_run_at`), `resolve_target`, `post_message(..., client_msg_id=f"sched:{id}:{occurrence.isoformat()}", source_channel="schedule")` с партом из Decision 2; затем публикация: при новой/восстановленной сессии — `publish_users(members, conversation.updated)`; `message.created` через `publish` + `publish_users` (как `invite_member` в `agent/tools.py:551-559`); `memory_indexer.kick()`; `get_runtime().schedule(conv.id, msg.id, wait=True)`.
   - `fire_due(now) -> int`: выбрать `next_run_at <= now` (без paused-в-будущем), для каждой: вычислить `occurrence = next_run_at`, новый `next_run_at = next_after(now)`, `last_run_at`, `fire_count += 1`, commit (claim) → если `now - occurrence <= MISSED_GRACE` — `fire()`, иначе warning; если новый `next_run_at is None` — удалить строку после `fire()`.
   - **Verify:** тесты (sqlite + TestModel binding, как `test_agent.py`): command daily → два `fire_due` создают две разные сессии `origin=schedule`; command hourly → одна сессия переиспользуется, после archive/delete — новая; reminder → в исходную сессию, archived восстанавливается в active, deleted → новая личная, `job.conversation_id` обновлён; reminder, созданный в сессии планировщика, срабатывает там же; повтор `fire` с тем же occurrence не дублирует сообщение; once удаляется после срабатывания; пропуск > 12 ч не постит, ≤ 12 ч постит один раз; пауза блокирует срабатывания до `until`. Ран реально выполняется (assistant-сообщение появилось после `get_runtime().wait_idle()`).

4. **Воркер — `schedule/worker.py`** по образцу `memory/indexer.py`: `start_worker()`, `stop_worker()`, `kick()`, `_loop()`: `fire_due(utcnow())` в try/except+log, затем ожидание `kick` с таймаутом `min(до ближайшего next_run_at, 60 с)` (минимум 1 с). Подключить в `api/app.py lifespan` (start после indexer, stop до `dispose_engine`).
   - **Verify:** `uv run pytest` целиком зелёный (тесты не стартуют lifespan-воркер — `kick()` no-op без воркера, как у indexer); ручная проверка шага 9.

5. **Рантайм — `agent/runtime.py`.**
   - `schedule(conversation_id, trigger_message_id, wait: bool = False)` и `maybe_run(..., wait=False)`: при `wait=True` ждать lock (`async with lock`) вместо раннего `return`.
   - В `_run` триггер с `scheduled`-партом считается упоминанием (обход `reply_mode == "mention"`).
   - `_content`: текст-парт с `scheduled` рендерится как `[Сработало напоминание #<id> «<title>»] Напомни пользователю: <text>` / `[Событие по расписанию #<id> «<title>»] <text>` — агент понимает контекст и id паттерна (для «приостанови это»).
   - **Verify:** тест: в group-сессии с `reply_mode=mention` scheduled-триггер запускает ран; два scheduled-триггера подряд в одной сессии дают два ответа ассистента (второй дождался lock).

6. **Инструменты агента** — `schedule/tools.py`, регистрация в `CORE_TOOLS` (`agent/tools.py:668`):
   - `schedule_task(ctx, kind: Literal["command","reminder"], title: str, text: str, schedule: ScheduleSpec) -> dict` — docstring объясняет: `command` — что консьерж выполнит от имени пользователя (самодостаточная формулировка), `reminder` — о чём напомнить в этой сессии; «через N минут» → `delay_minutes`; даты в локальном времени семьи. Возвращает `{id, kind, describe, next_runs: [3 локальных ISO], session_mode}` или `{error}`.
   - `list_scheduled_tasks(ctx)` — свои паттерны (id, kind, title, text, describe, next_run local, paused_until local).
   - `pause_scheduled_task(ctx, task_id: int, until: datetime)`, `resume_scheduled_task(ctx, task_id)`, `cancel_scheduled_task(ctx, task_id)`.
   - Все — только по `ctx.deps.user_id`; чужой id → `{error: "not found"}`.
   - **Verify:** тесты прямого вызова инструментов с фейковым `RunContext` (шаблон в `test_agent.py`) + `test_tools_deny_list`-подобный тест: guest не видит инструменты; `uv run ruff check . && uv run mypy src`.

7. **REST — `api/schedule.py`** (роутер в `api/app.py`): `GET /schedules` → `list[ScheduleOut]` (`id, kind, title, text, describe, session_mode, conversation_id, next_run_at, paused_until, last_run_at, fire_count, created_at`); `PATCH /schedules/{id}` `{paused_until: datetime | null}` (null = возобновить); `DELETE /schedules/{id}` → 204. Только свои (иначе 404). Аудит — как в соседних роутерах, если там пишется `audit_log` на мутации.
   - **Verify:** API-тесты через `client` фикстуру: список, пауза/возобновление пересчитывают `next_run_at`, delete удаляет, чужой → 404.

8. **Telegram-sink**: `channels/telegram/sink.py:_message_created` — не зеркалить сообщения с `source_channel == "schedule"` (ответ ассистента по-прежнему уходит).
   - **Verify:** тест в `test_telegram.py`-стиле или точечный в `test_schedule.py`: scheduled-триггер в TG-привязанной сессии не попадает в outbox, ответ ассистента — попадает.

9. **PWA.**
   - `web/src/lib/api.ts`: тип `ScheduleOut`, `apiClient.listSchedules()`, `updateSchedule(id, {paused_until})`, `deleteSchedule(id)`.
   - `SettingsView.tsx`: секция `SchedulesSection` («Расписание», видна всем): карточки — иконка вида (команда/напоминание), title, text, `describe`, «следующее: …» (локальное время браузера из UTC, как `expires` в `TelegramSection`), бейдж «пауза до …»; кнопки «Пауза до…» (`<input type="datetime-local">`), «Возобновить», «Удалить» (confirm), ссылка на сессию при `conversation_id`. Пустое состояние: «Попросите консьержа: „напомни через 10 минут…“». Подзаголовок шапки — «Telegram · Расписание».
   - `ChatView.tsx`/`MessageParts.tsx`: сообщение, чей text-парт содержит `scheduled`, рендерится системной строкой `⏰ «title»: text` (Decision 12).
   - **Verify:** `cd web && pnpm lint && pnpm build`; вручную в `pnpm dev`: секция отображает созданный через агента паттерн, пауза/удаление работают.

10. **Документация и деплой-проверка.** Раздел «Планировщик (события по таймеру)» в `AGENTS.md`: таблица, правила сессий, инструменты, REST, коалесцирование/grace, отсутствие внешних уведомлений. Деплой `deploy/deploy.sh` (миграция 0013 применится).
   - **Verify:** `cd server && uv run ruff check . && uv run mypy src && uv run pytest`; после деплоя `curl -k https://$CONCIERGE_DOMAIN/healthz` → ok; в PWA «напомни через 2 минуты проверить духовку» → через ~2 мин в той же сессии появляется ⏰-строка и ответ консьержа; `journalctl -u concierge` без ошибок воркера.

## Out of scope

- Внешние уведомления (Telegram DM-пинг, web push) о срабатываниях.
- Создание/редактирование паттернов из UI (только агент), редактирование существующего паттерна (отмена + создание заново), админ-обзор чужих паттернов.
- Синхронизация `scheduled_jobs` в `concierge_analytics`.
- Привязка новых сессий планировщика к TG `dm_current`.
- Сырой RRULE/cron-ввод.

## Open risks

- **Стоимость частых команд**: `minutely` команда — ран LLM каждую минуту. Ограничено только лимитом числа паттернов; если нужен минимальный интервал для `command` — уточнить у пользователя, а не вводить самовольно.
- **DST**: семейная TZ (Kaliningrad) без перехода; для `minutely/hourly` через DST в других зонах возможны сдвиги wall-clock — приемлемо, не чинить.
- **Типизация `ScheduleSpec` как параметра инструмента**: если pydantic-ai/провайдер плохо переваривает вложенную модель в схеме (ошибки валидации у реальной модели), допустимо развернуть поля спецификации в плоские параметры `schedule_task` — это не меняет остальной план; при другом расхождении — `blocked`.
- **Один процесс**: воркер in-process; при переходе на >1 uvicorn-воркера нужен advisory-lock/`SKIP LOCKED` в `fire_due` (в hub уже аналогичная оговорка).
- `post_message` выставляет владельцу `last_read_seq` на триггер — непрочитанным будет только ответ ассистента; это желаемо.

## How to execute

---
**Execute this plan in a fresh context**

Start a clean session — `/clear` in this terminal, or `claude` in a new one — then paste:

```
Implement the plan at .claude/plans/2026-11-18-scheduled-events.md.
Pre-flight, before any change:
1. Read the plan. For every id in depends_on, read .claude/plans/*<id>.md: its status must be `done`. If any is not, stop and tell me which ones and their status.
2. For every other plan with status `in_progress`: if its id is in conflicts_with, or its `touches` overlap ours, stop and tell me which plan and which files.
3. Run `git status --short`: if there are uncommitted changes in our `touches`, stop and tell me.
4. Claim the plan: set status: in_progress, claimed_by: <this session / branch>, claimed_at: <now> in its header.
Then work the steps in order and run each step's Verify check before moving on.
If a step turns out to be wrong, blocked, or ambiguous, set status: blocked with blocked_reason, stop, and tell me instead of improvising.
After the last Verify passes, set status: done and finished_at: <now>.
```
