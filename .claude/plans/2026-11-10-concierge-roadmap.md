# Семейный агент-консьерж: дорожная карта разработки

Python-сервер (FastAPI + PydanticAI + PostgreSQL + Qdrant на box) с ролевой авторизацией, личными и групповыми сессиями, личной/общей памятью, мультимодальным вводом, голосовой быстрой сессией, Telegram-ботом, генерацией документов, делегированием кода в devin/claude/pi по SSH на Mac и адаптивным PWA-клиентом на React — разбито на 12 последовательных планов (0–11).

```yaml
---
id: concierge-roadmap
title: Семейный агент-консьерж — дорожная карта (планы 0–11)
planned: 2026-11-10
status: done
depends_on: []
conflicts_with: []
touches:
  - "**"            # greenfield: репозиторий ~/projects/concierge пуст
claimed_by: null    # статусы планов — в их секциях ниже
claimed_at: null
finished_at: 2026-11-11   # планы 0–11 done
blocked_reason: null
---
```

## Goal

Работающий на `box` сервер-консьерж, к которому члены семьи подключаются через адаптивное PWA (iPhone, Android, iPad, desktop) с нескольких устройств одновременно и через Telegram-бота (в т.ч. голосом); у каждого — личные сессии и память, есть групповые сессии (несколько людей + ИИ) и общая семейная память в Qdrant; одним нажатием кнопки «микрофон» в PWA создаётся новая сессия, голосовой запрос распознаётся и выполняется; администратор (глава семьи) управляет пользователями, ролями, системным промптом, списком ИИ-провайдеров, MCP-серверов и назначением «какая модель для какой цели»; в сессию можно отправлять аудио/видео/изображения/документы, ИИ умеет создавать документы; задачи на написание кода делегируются CLI-агенту (devin по умолчанию, настраивается: claude, pi), запускаемому по SSH на Mac.

## Relations

Реестра `.claude/plans/` в проекте не было (создан при сохранении этого плана), других планов/веток/worktree нет — план самостоятельный. Смежные проекты пользователя, из которых берутся паттерны (только чтение):
- `~/projects/telegram-ai-agent` — uv, pydantic-settings, **aiogram 3**, deepgram, mcp, ruff/mypy/pytest; обработчики `voice.py`, `photo.py`, `forum_topic.py`, `streaming.py`, middleware `auth.py` — образец Telegram-бота на Python.
- `~/.hermes/hermes-agent` — референс UI и архитектуры: `plugins/platforms/telegram/adapter.py` (allowlist `TELEGRAM_ALLOWED_USERS`, pairing-код для неавторизованных DM, forum-topics → отдельные сессии, голосовые сообщения → транскрипция, MarkdownV2, стриминг правками сообщения), `apps/desktop` (React + nanostores), системный промпт собирается один раз на сессию, память через Qdrant.

## Context (обследованное окружение)

- **box** (LAN-хост, ALT Linux 6.12, x86_64, 4 CPU, **3.7 GiB RAM**, 146 GB свободно, Python 3.12.13). Нет Docker, нет Postgres. Qdrant — systemd `qdrant.service`, порты 6333/6334, коллекции: `hermes_memories` (384-dim, named vector `dense`, payload `session_id`,`source`), `family_archive` (1024-dim, payload `doc_id`,`doc_type`,`persons`,`places`,`themes`,`page_start`), `obsidian_notes`. SSH `box` работает без пароля (BatchMode).
- **Mac** (этот): `devin`, `claude` в `~/.local/bin`; `uv 0.10`, Python 3.14, node 26, pnpm, docker. `pi` не установлен. Hermes настроен на локальную модель `deepseek-heretic` через `http://<lan-host>:11434/v1` (Ollama на другом Mac).
- Ограничение RAM на box: Postgres (~200 MB) + сервер (~300–500 MB) + Qdrant ок; **локальные эмбеддинги на box только лёгкие** (fastembed `all-MiniLM-L6-v2`, 384-dim) либо через внешний провайдер — выбирается через capability `embedding`.
- PWA требует HTTPS (service worker, `getUserMedia`) → обратный прокси с TLS (Caddy с internal CA или Tailscale HTTPS). Для Telegram-бота на box нужен исходящий доступ к `api.telegram.org` (long polling — входящих портов не нужно).

## Decisions (зафиксировано)

| Решение | Выбор | Отвергнуто / почему |
|---|---|---|
| Хостинг | Сервер на box; кодинг-агенты на Mac по SSH | Всё на Mac (не 24/7); всё на box (CLI-агенты и их OAuth-логины живут на Mac) |
| Основная БД | PostgreSQL (на box, порт только localhost) | SQLite — пользователь выбрал PG; JSONB, tsvector-FTS, конкурентность |
| Векторы/память | Qdrant на box, **новые** коллекции `concierge_*`; существующие не трогаем | Переиспользование `hermes_memories` — другая схема payload и чужой владелец |
| Агентное ядро | PydanticAI (`Agent`, `tools`, `MCPServerStdio`/`MCPServerStreamableHTTP`, провайдеры OpenAI/Anthropic/Ollama/Google/Mistral) | Свой цикл + LiteLLM; LangGraph |
| Web | FastAPI + uvicorn, WebSocket, SQLAlchemy 2 async + Alembic, pydantic-settings | — |
| Frontend | React + Vite + TypeScript, PWA (vite-plugin-pwa), **адаптивный (mobile-first) дизайн**: iPhone/Android (одна колонка, нижняя панель, safe-area), iPad/desktop (двух-/трёхколоночный layout: список сессий + чат + боковая панель); UI по образцу Hermes desktop | Svelte, Vue; отдельные мобильное и десктопное приложения |
| Пакетный менеджер / качество | `uv`, `ruff`, `mypy`, `pytest(-asyncio)` — как в `telegram-ai-agent` | — |
| Авторизация | Пароль (argon2id) + refresh-токен на устройство (httpOnly cookie) + короткий JWT access; приглашение по одноразовой ссылке от админа; WebAuthn — позже | OAuth внешних провайдеров |
| Хранение настроек | PG `settings(scope, key, value JSONB, version)`; секреты — `.env`/шифрование Fernet; валидация pydantic; горячая перезагрузка по `version` | YAML-файлы |
| Сессии | Бессрочно в PG; `status=active|archived`; архив = флаг + авто-саммари, остаётся в FTS и Qdrant | Удаление/вынос |
| Поведение ИИ в группе | `reply_mode`: `always` (личные) / `mention` (групповые по умолчанию) | Всегда отвечать в группе — шум |
| Роли | `admin`, `adult`, `child`, `guest` как данные (`roles.permissions` JSONB + `prompt_fragment`) | Хардкод |
| CLI code-agent | `CodeAgentBackend`: `devin`, `claude`, `pi`; выполнение через `asyncssh` на Mac; неинтерактивный режим | tmux-обёртка как в telegram-ai-agent |
| Capabilities | Фиксированный словарь: `chat`, `chat_fast`, `vision`, `stt`, `tts`, `embedding`, `document`, `code_agent`; привязка в PG, переопределение по роли | Произвольные цели |
| **Голосовая быстрая сессия** | Кнопка «микрофон» на главном экране PWA: запись (push-to-talk или tap-start/tap-stop) → загрузка аудио → серверный `stt` → **автоматически** создаётся новая личная сессия, транскрипт сохраняется как первое сообщение пользователя (с аудио-вложением) и сразу запускается run; транскрипт редактируем постфактум (отправить исправленный) | Браузерный `SpeechRecognition` (нестабилен на iOS/Android, нет в PWA-режиме Safari); подтверждение транскрипта перед отправкой (замедляет сценарий «одно нажатие») |
| **Telegram-бот** | Один бот семьи на aiogram 3, long polling из процесса сервера (отдельная asyncio-задача, включается настройкой); привязка Telegram-аккаунта к пользователю консьержа через одноразовый код из PWA (`/start <код>`), админ также может привязать вручную; неизвестные пользователи получают только ответ с инструкцией привязки (как pairing в Hermes); DM = личные сессии (`/new` открывает новую, текущая — последняя активная), forum-topic в супергруппе = групповая сессия, участники — привязанные пользователи; голосовые/аудио/фото/документы проходят тот же media-пайплайн; ответы — MarkdownV2 со стримингом правками сообщения; опциональный голосовой ответ через `tts`, если включён | Webhook (нужен входящий порт/туннель); отдельный процесс бота (дублирование auth/БД); несколько ботов на пользователя |

## Общая архитектура (целевое состояние)

```
PWA (React, адаптивное) ──HTTPS/WSS──> Caddy ──> FastAPI (uvicorn, box)
Telegram ──long polling (aiogram)──────────────┤
                                       ├─ auth / rbac / settings / telegram_links ──> PostgreSQL (localhost)
                                       ├─ conversations hub (WS fanout, presence, streaming)
                                       ├─ channel adapters: web (WS), telegram (aiogram) — общий ChannelEvent
                                       ├─ agent runtime (PydanticAI)  ──> провайдеры LLM (OpenAI/Anthropic/Ollama…)
                                       │    ├─ tools: memory, documents, attachments, code_agent
                                       │    └─ MCP clients (stdio / streamable-http)
                                       ├─ memory service                ──> Qdrant (box:6333)
                                       ├─ media pipeline (ffmpeg, pymupdf, STT/vision/TTS через capabilities)
                                       └─ code-agent adapter ──SSH──> Mac: devin | claude | pi
Blob-хранилище: /srv/concierge/blobs/<sha256> (вложения, сгенерированные документы, голосовые)
```

### Схема данных PostgreSQL (ядро)

- `users(id, username, display_name, role_id, password_hash, is_active, prompt_fragment, created_at)`
- `roles(id, name, permissions JSONB, prompt_fragment, created_at)` — permissions: `{"settings": "rw|r|none", "manage_users": bool, "tools": {"allow": [...], "deny": [...]}, "mcp_servers": [...], "capabilities_override": {...}, "max_upload_mb": int, "code_agent": bool, "telegram": bool, "voice_quick_session": bool}`
- `invites(id, token_hash, role_id, created_by, expires_at, used_by)`
- `devices(id, user_id, name, refresh_token_hash, last_seen_at, revoked_at)`
- `settings(scope ENUM(global,role,user), scope_id, key, value JSONB, version, updated_by, updated_at)`
- `model_providers(id, name, kind, base_url, api_key_ref, enabled)`
- `capability_bindings(capability, provider_id, model, params JSONB, role_id NULL)`
- `mcp_servers(id, name, transport, command/url, env_ref, enabled, allowed_tools)`
- `code_agents(id, name ENUM(devin,claude,pi), ssh_host, workdir, args JSONB, is_default)`
- `conversations(id, kind ENUM(personal, group), title, owner_id, status, reply_mode, origin ENUM(web, voice_quick, telegram), summary, settings JSONB, created_at, updated_at, archived_at)`
- `conversation_members(conversation_id, user_id, joined_at, last_read_seq)`
- `messages(id, conversation_id, seq, author_kind, author_id, parts JSONB, run_id, source_channel ENUM(web, telegram), created_at, tsv tsvector GENERATED)`
- `runs(id, conversation_id, trigger_message_id, status, model, usage JSONB, started_at, finished_at, error)`
- `attachments(id, sha256, mime, size, original_name, uploaded_by, conversation_id, derived JSONB)`
- `memory_items(id, scope ENUM(user, family), owner_id, text, source_message_id, kind, created_at, deleted_at)`
- `telegram_links(user_id, telegram_user_id UNIQUE, telegram_username, linked_at)`; `telegram_link_codes(code_hash, user_id, expires_at)`
- `telegram_bindings(chat_id, thread_id NULL, conversation_id, kind ENUM(dm_current, topic, group))` — соответствие Telegram-чата/топика сессии; `telegram_messages(message_id, chat_id, conversation_message_id)` для правок при стриминге и ответов reply.

### Коллекции Qdrant (новые)

- `concierge_memory` — факты/заметки (scope user|family), фильтр по `scope`/`owner_id` при каждом поиске.
- `concierge_messages` — чанки сообщений всех сессий (в т.ч. архивных), payload `conversation_id`, `member_ids[]`.
- Имя коллекции с суффиксом модели (`concierge_memory__minilm384`), смена модели = новая коллекция + реиндекс.

## Планы по порядку реализации

Выполнять последовательно; каждый план — отдельная сессия реализации. Документ сохраняется как `.claude/plans/2026-11-10-concierge-roadmap.md`; при желании каждый план выделяется в файл `.claude/plans/…-concierge-NN-<slug>.md` с `depends_on` на предыдущий.

### План 0 — Скелет репозитория и инфраструктура на box

**Status: done — 2026-11-10, devin-desktop/main.**

1. Монорепо `concierge/`: `server/` (uv, `pyproject.toml` с ruff/mypy/pytest, `src/concierge/…`), `web/` (pnpm, Vite), `deploy/` (systemd unit, Caddyfile, скрипты), `AGENTS.md` с командами проверки.
2. Пакеты сервера: `concierge.core`, `concierge.auth`, `concierge.settings`, `concierge.conversations`, `concierge.channels` (web, telegram), `concierge.agent`, `concierge.memory`, `concierge.media`, `concierge.documents`, `concierge.codeagent`, `concierge.api`, `concierge.ws`.
3. Установка на box: PostgreSQL (пакет ALT, `listen_addresses='localhost'`, БД `concierge`), ffmpeg, `uv`; Caddy с internal TLS (или Tailscale) → `https://concierge.<lan>`; systemd `concierge.service` (`EnvironmentFile=/etc/concierge/.env`, `MemoryMax=1.2G`).
4. Alembic с первой пустой миграцией; `GET /healthz` проверяет PG и Qdrant.
5. `deploy/deploy.sh` (rsync → `uv sync` → `alembic upgrade head` → `systemctl restart`).
   **Verify:** `curl -k https://box/healthz` → `{"db":"ok","qdrant":"ok"}`; `uv run ruff check . && uv run mypy src && uv run pytest` проходят.

### План 1 — Пользователи, роли, авторизация, настройки

**Status: done — 2026-11-10, devin-desktop/main.**

1. Миграция: `users`, `roles`, `invites`, `devices`, `settings`, `model_providers`, `capability_bindings`, `mcp_servers`, `code_agents`. Сид ролей.
2. CLI `concierge bootstrap-admin`.
3. Auth API: `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me`, `GET/DELETE /auth/devices`; `POST /invites` (admin) → `POST /auth/register?token=`.
4. RBAC-зависимость `require(permission)`; admin = `*`.
5. Settings service (слои global → role → user, кэш по `version`); API `GET/PUT /settings/{scope}`; секреты шифруются, в API маскируются.
6. Admin API: CRUD users/roles, провайдеров, capability-bindings, MCP, code-agents.
   **Verify:** pytest: login/refresh/revoke; `child` → 403 на `PUT /settings/global`; приглашение → регистрация → логин; смена binding `chat` без рестарта.

### План 2 — Сессии, сообщения, реалтайм

**Status: done — 2026-11-10, devin-desktop/main.**

1. Миграция: `conversations` (с `origin`), `conversation_members`, `messages` (+ `source_channel`, GIN на `tsv`), `runs`, `attachments`.
2. REST: CRUD сессий, членство, архивация, `GET /conversations/{id}/messages?before_seq=`, `GET /search?q=`.
3. Абстракция `ChannelEvent`/`ChannelSink` в `concierge.channels` — общий вход сообщений и выход событий для web и (позже) Telegram.
4. WebSocket `/ws`: подписка на сессии; события `message.created`, `message.delta`, `run.status`, `presence`, `typing`, `conversation.updated`; hub в памяти (один воркер; при росте — LISTEN/NOTIFY).
5. Идемпотентность (`client_msg_id`), `last_read_seq`.
   **Verify:** два WS-клиента одного пользователя + третий другого участника получают одно `message.created`; архивная сессия находится через `/search`.

### План 3 — Агентное ядро (PydanticAI), промпт, инструменты, MCP

**Status: done — 2026-11-10, devin-desktop/main.**

1. `concierge.agent.providers`: `get_model(capability, user)` из `model_providers`+`capability_bindings`.
2. `PromptComposer`: базовый промпт + фрагмент роли + фрагмент пользователя + контекст сессии + блок памяти; собирается один раз на run.
3. `AgentRuntime.run(conversation, trigger_message)`: история → `Agent.run_stream` → дельты в `ChannelSink` → сохранение; один run на сессию; `reply_mode`.
4. Инструменты ядра минимум; фильтрация по `roles.permissions.tools`.
5. MCP-клиенты из `mcp_servers`, allowlist по ролям, вызовы как `tool`-сообщения.
6. Заголовок сессии через `chat_fast`.
   **Verify:** e2e с `TestModel`: стрим по WS и сохранение; deny-лист инструментов; тестовый stdio-MCP вызывается.

### План 4 — PWA MVP (React + Vite + TS, адаптивное)

**Status: done — 2026-11-10, devin-desktop/main, finished_at: 2026-11-10.**

1. `web/`: Vite + React + TS, `vite-plugin-pwa` (manifest с иконками/`display: standalone`, offline-shell), роутер, nanostores, API-клиент с авто-refresh, WS-клиент с реконнектом/ресинком по `seq`.
2. **Адаптивный layout** (CSS container/media queries, `100dvh`, `env(safe-area-inset-*)`): телефон (<768px) — одна колонка, выдвижной список сессий, нижняя панель ввода, крупные тач-цели; планшет (768–1024, iPad в обоих ориентациях) — две колонки (список + чат); desktop (>1024) — три (список + чат + боковая панель с деталями/участниками/памятью). Тёмная/светлая тема по системной.
3. Экраны: вход/регистрация по приглашению; список сессий (личные/групповые/архив); чат в стиле Hermes (markdown, стриминг, tool-calls, статус run); создание групповой сессии; поиск.
4. Многоустройственность: онлайн/печатает, синхронизация прочитанного.
5. Раздача статики через Caddy; API под `/api`.
   **Verify:** Lighthouse PWA installable; ручная проверка на iPhone Safari («На экран Домой»), Android Chrome, iPad (портрет/ландшафт), desktop — без горизонтального скролла, клавиатура не перекрывает поле ввода; два браузера одного пользователя видят стрим одновременно.

### План 5 — Память (Qdrant): личная и общая

**Status: done — 2026-11-10, devin-desktop/main, finished_at: 2026-11-10.**

1. `EmbeddingService` через capability `embedding`; коллекции с суффиксом модели; payload-индексы.
2. `memory_items`; инструменты `remember/recall/forget`; запись в `family` — permission.
3. Фоновый индексатор сообщений (outbox); архивация → саммари → индекс.
4. Retrieval при run: личная память участников + семейная + прошлые сессии, с фильтрами доступа.
5. UI «Память».
   **Verify:** факт A не виден B; архивный диалог цитируется; тест фильтров payload.

### План 6 — Мультимодальный ввод (STT, vision, видео, документы)

**Status: done — 2026-11-10, devin-desktop/main, finished_at: 2026-11-10.**

1. `POST /attachments` (multipart, лимит по роли, дедуп sha256), `GET /attachments/{id}` с проверкой членства.
2. `MediaPipeline` (фон, результат в `attachments.derived` + событие WS): изображение → `vision`; аудио (webm/opus, ogg/opus из Telegram, m4a с iOS) → ffmpeg-нормализация → `stt` (Deepgram / OpenAI-совместимый whisper — настраивается); видео → аудиодорожка → `stt`, кадры → `vision`; документы → pymupdf/python-docx/openpyxl/python-pptx/текст.
3. Публичный сервисный метод `transcribe(attachment_id) -> str` — используется планами 7 и 8.
4. PWA: прикрепление файлов, запись голоса в сообщении (MediaRecorder; на iOS — `audio/mp4`), камера, превью.
   **Verify:** голосовое → транскрипт в сообщении, ответ учитывает; PDF 30 стр. → текст; фикстурные тесты пайплайна (включая `.ogg` opus и `.m4a`).

### План 7 — Голосовая быстрая сессия («кнопка микрофона»)

**Status: done — 2026-11-11, devin-desktop/main, finished_at: 2026-11-11.**

1. API `POST /conversations/voice-quick` (multipart аудио или `attachment_id`): проверка permission `voice_quick_session` → создание личной сессии `origin=voice_quick` → `transcribe` → если транскрипт пустой/короче порога — сессия помечается на удаление и клиенту возвращается `empty_transcript` → иначе сообщение пользователя `parts=[text(транскрипт), audio_ref]` → запуск `AgentRuntime.run` → ответ `{conversation_id, message_id}`; клиент подписывается на сессию по WS и видит стрим.
2. PWA: большая кнопка микрофона на экране списка сессий и в нижней панели (на телефоне — FAB; на desktop — в шапке и горячая клавиша); режимы push-to-talk (удержание) и tap-start/tap-stop (настройка пользователя); индикатор уровня, таймер, лимит длительности (настройка, по умолчанию 120 с); после ответа сервера — переход в новую сессию; транскрипт отображается как первое сообщение с пометкой «распознано», действие «исправить и отправить заново».
3. Заголовок сессии — из транскрипта (`chat_fast`), для коротких запросов — усечённый текст.
4. Опционально по настройке `voice_quick.tts_reply`: ответ озвучивается через capability `tts` (если binding есть) и автопроигрывается.
5. Обработка ошибок: STT-провайдер недоступен → сессия создаётся с аудио-вложением без текста и сообщением системы «не удалось распознать», пользователь может повторить распознавание.
   **Verify:** на iPhone/Android в PWA-режиме: нажать микрофон → сказать запрос → новая сессия с распознанным текстом и потоковым ответом за одно действие; интеграционный тест эндпоинта с фикстурным `.m4a` и mock-STT; тест `empty_transcript`.

### План 8 — Telegram-бот (аналог Hermes gateway)

**Status: done — 2026-11-11, devin-desktop/main, finished_at: 2026-11-11.**

1. Миграция: `telegram_links`, `telegram_link_codes`, `telegram_bindings`, `telegram_messages`. Настройки `telegram.enabled`, `telegram.bot_token` (секрет), `telegram.allowed_user_ids` (доп. allowlist, опционально), `telegram.voice_reply` (off/on-request/always).
2. `concierge.channels.telegram` на aiogram 3: запуск long polling как задача приложения FastAPI (lifespan) при `telegram.enabled`; middleware авторизации: `from_user.id` → `telegram_links` → пользователь консьержа; неизвестный пользователь получает сообщение с инструкцией «получите код в приложении → `/start <код>`» и игнорируется далее (rate-limit).
3. Привязка: в PWA (профиль) кнопка «Привязать Telegram» генерирует код (TTL 10 мин), `/start <код>` создаёт `telegram_links`; admin в панели видит/удаляет привязки и может привязать по известному `telegram_user_id`. Permission `telegram` на роли.
4. Маршрутизация чатов → сессии: DM → `dm_current` (последняя активная личная сессия пользователя с `origin=telegram` или любая, выбранная `/sessions`); `/new` — новая сессия; `/sessions` — inline-клавиатура последних сессий; супергруппа с топиками — каждый топик = групповая сессия, участники добавляются при первом сообщении привязанного пользователя (непривязанные — игнорируются); группа без топиков — одна групповая сессия на чат. `reply_mode=mention` в группах учитывает упоминание бота и reply на его сообщение.
5. Входящие медиа: voice/audio/video/photo/document → скачивание через Bot API → `POST /attachments`-эквивалент → `MediaPipeline`/`transcribe` → сообщение с частями (голосовое становится текст + audio_ref, как в плане 7). Голосовое в DM без текста работает как быстрый запрос.
6. Исходящие: `TelegramSink` реализует `ChannelSink`: стрим — редактирование одного сообщения с троттлингом (~1 с), MarkdownV2 с экранированием и разбиением >4096; tool-calls — краткие статусы; файлы/документы, сгенерированные ИИ, отправляются как `sendDocument`; при `telegram.voice_reply` ответ озвучивается `tts` и отправляется `sendVoice` (ogg/opus через ffmpeg). Сообщения из PWA в ту же сессию зеркалятся в привязанный чат/топик, и наоборот (двусторонняя синхронизация через hub).
7. Команды: `/start`, `/new`, `/sessions`, `/archive`, `/voice on|off`, `/help`; команды регистрируются через `setMyCommands`.
8. Admin UI: включение бота, токен, привязки, журнал.
   **Verify:** привязка через код; текст и голосовое в DM → ответ в Telegram и то же сообщение видно в PWA; топик в тестовой супергруппе ↔ групповая сессия с двумя привязанными пользователями; непривязанный пользователь получает только инструкцию; тесты aiogram-хэндлеров с mock Bot API и mock-STT.

### План 9 — Генерация документов ИИ

**Status: done — 2026-11-11, devin-desktop/main, claimed_at: 2026-11-11, finished_at: 2026-11-11.**

1. Инструмент `create_document(format, title, content)`: `md`, `txt`, `csv`, `docx`, `xlsx`, `pptx`, `pdf` (markdown → HTML → PDF через pymupdf; weasyprint — только если памяти хватит).
2. Результат — `attachments` от ассистента, карточка в чате (PWA) и `sendDocument` (Telegram).
3. Capability `document` — опционально отдельная модель.
   **Verify:** «список покупок в xlsx» → валидный файл; тесты обратного чтения форматов.

### План 10 — Делегирование кода CLI-агенту (devin → claude → pi)

**Status: done — devin-desktop/main, claimed_at: 2026-11-11, finished_at: 2026-11-11.**
Отклонения: Devin через `devin -p` вместо `devin acp` (acp требует отдельный API key; `-p` без стрима — прогресс heartbeat'ом), pi — `--mode json`; ClaudeBackend отложен по решению пользователя, «смена без рестарта» проверена переключением devin → pi. Живой Verify: задача из PWA-чата выполнена devin, затем pi на Mac, результат в чате.

1. `CodeAgentBackend` (Protocol): `start/stream_events/cancel`; `DevinBackend`, `ClaudeBackend` (`claude -p --output-format stream-json`), `PiBackend`; флаги headless-режима devin/pi проверить в начале плана.
2. Транспорт `asyncssh` к Mac; allowlist рабочих каталогов.
3. Инструмент `delegate_coding_task(task, project)` (permission `code_agent`); прогресс → `tool`-сообщения/`run.status` во все каналы.
4. Admin UI: дефолтный агент, проекты.
   **Verify:** задача из чата → CLI на Mac выполняется, лог стримится, результат в чате; смена на `claude` без рестарта.

### План 11 — Админ-панель, эксплуатация, безопасность (финал)

**Status: done — devin-desktop/main, claimed_at: 2026-11-11, finished_at: 2026-11-11.**

1. PWA-разделы администратора (адаптивные, на телефоне — списки/формы в одну колонку): пользователи/роли/права, редактор промпта с предпросмотром, провайдеры и bindings, MCP, code-agents, Telegram, журнал runs/затрат.
2. Эксплуатация: `pg_dump` + rsync blobs по cron, снапшоты Qdrant, ротация логов, `/metrics`.
3. Безопасность: rate-limit логина и Telegram-pairing, CSP, лимиты размеров, аудит-лог, доступ к Qdrant только с localhost (согласовать — им пользуется Hermes с Mac).
   **Verify:** админ настраивает систему через UI без SSH; восстановление из бэкапа на чистой БД.

## Out of scope

- Сторонние агенты по A2A/ACP (слот `concierge.agent.external`).
- Web Push-уведомления (после PWA MVP при необходимости; Telegram частично закрывает потребность).
- Другие мессенджеры (Discord/Slack и т.д.) — архитектура `ChannelSink` позволяет добавить позже.
- Мультиворкерный деплой.
- Миграция `hermes_memories`/`family_archive` (возможен read-only источник в плане 5 отдельным решением).

## Open risks

- **RAM box 3.7 GiB**: при проблемах — эмбеддинги/STT только внешние, `MemoryMax`, swap; weasyprint не ставить.
- Headless-режимы devin/pi не проверены; fallback — `claude -p` как дефолт.
- PydanticAI: зафиксировать версию с верхней границей, проверить MCP API в начале плана 3.
- **iOS PWA и микрофон**: `getUserMedia`/MediaRecorder в standalone-режиме Safari работают с iOS 14.3+, формат `audio/mp4`; фоновая запись невозможна; экран должен быть активен. Тестировать на реальном iPhone в планах 6–7.
- Telegram: лимиты Bot API на `editMessageText` (стриминг с троттлингом), размер файлов 20 MB на скачивание / 50 MB на отправку; при двусторонней синхронизации возможны дубли — опираться на `telegram_messages` и `client_msg_id`.
- Qdrant на box открыт на 0.0.0.0 и используется Hermes — смена политики требует согласования (план 11).
- Один uvicorn-воркер: hub в памяти; при >1 — LISTEN/NOTIFY.
- Объём: 12 планов — большой проект; каждый план выполнять отдельной сессией, не объединять.

## How to execute

---
**Execute this plan in a fresh context**

Start a clean session — `/clear` in this terminal, or `claude` in a new one — then paste:

```
Implement the plan at .claude/plans/2026-11-10-concierge-roadmap.md, план 0 (затем по одному плану за сессию: 1, 2, …, 11).
Pre-flight, before any change:
1. Read the plan. For every id in depends_on, read .claude/plans/*<id>.md: its status must be `done`. If any is not, stop and tell me which ones and their status.
2. For every other plan with status `in_progress`: if its id is in conflicts_with, or its `touches` overlap ours, stop and tell me which plan and which files.
3. Run `git status --short`: if there are uncommitted changes in our `touches`, stop and tell me.
4. Claim the plan: set status: in_progress, claimed_by: <this session / branch>, claimed_at: <now> in its header.
Then work the steps in order and run each step's Verify check before moving on.
If a step turns out to be wrong, blocked, or ambiguous, set status: blocked with blocked_reason, stop, and tell me instead of improvising.
After the last Verify passes, set status: done and finished_at: <now>.
```
---
