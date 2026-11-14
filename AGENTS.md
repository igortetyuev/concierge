# Concierge

Семейный агент-консьерж. Монорепо: `server/` — FastAPI + PydanticAI + PostgreSQL + Qdrant
(деплоится на выделенный хост по SSH), `web/` — адаптивное PWA (React + Vite + TS), `deploy/` —
systemd unit, Caddyfile, скрипты деплоя. Дорожная карта: `.claude/plans/2026-11-10-concierge-roadmap.md`.

## Проверки (server/)

```bash
cd server
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

## Проверки (web/)

```bash
cd web
pnpm install
pnpm lint
pnpm build
```

PWA: API проксируется с `/api` (dev/preview — vite proxy на `VITE_API_TARGET`,
prod — Caddy `handle_path`). Иконки: `node scripts/generate-icons.mjs`.

## Деплой

Параметры деплоя — `deploy/.env` (в .gitignore; шаблон — `deploy/.env.example`):
`CONCIERGE_REMOTE` (ssh-алиас хоста), `CONCIERGE_DOMAIN`, `CONCIERGE_DEST`,
`CONCIERGE_BACKUP_DIR`.

```bash
deploy/provision.sh   # одноразовая инициализация (PostgreSQL, Caddy, uv, systemd)
deploy/deploy.sh      # rsync -> uv sync -> alembic upgrade head -> restart
curl -k https://$CONCIERGE_DOMAIN/healthz   # -> {"db":"ok","qdrant":"ok"}
```

Конфигурация приложения на сервере — через env с префиксом `CONCIERGE_`
(`/etc/concierge/.env` на хосте).

HTTPS — `tls internal` (внутренний CA Caddy). Корневой сертификат отдаётся на
`/root.crt` (копируется из `…/pki/authorities/local/root.crt` в web-root при
provision/deploy) — установка на устройства нужна для mic/camera
(`navigator.mediaDevices` только в secure context).

## Эмбеддинги

Ollama на отдельном Windows-хосте в LAN: `http://<win-host>:11434/v1`, модель
`bge-m3` (1024-dim). На хосте NVIDIA GTX 1070 (8 ГБ), модели полностью в VRAM
(`/api/ps`: `size_vram` == `size`). На сервере capability `embedding` привязана к провайдеру
`ollama-lan` (bge-m3): коллекции Qdrant `concierge_memory__bgem3` и
`concierge_messages__bgem3`. Без binding `remember`/`recall` молча деградируют
(запись в `memory_items` есть, векторов нет) — смотри warning в journald.
Автозапуск на Windows — Run-ключ `Ollama`, `OLLAMA_HOST=0.0.0.0` в user env,
firewall-правило для порта 11434.
На сервере заведён провайдер `ollama-lan` (kind `ollama`, тот же base_url); capability `vision`
привязана к нему как `qwen3-vl:4b-instruct` (инференс на GPU).

## STT/TTS

Speaches 0.8.3 на том же Windows-хосте (venv `C:\Users\<user>\speaches-venv`,
cwd `C:\Users\<user>\speaches-home`, лог `speaches.log`):
`http://<win-host>:8000/v1`, whisper `large-v3-turbo` на GPU
(`WHISPER__INFERENCE_DEVICE=cuda` в `run-speaches.ps1`, GTX 1070;
~1 с на короткий клип против ~11 с на CPU; алиас `whisper-1`),
Kokoro TTS (`tts-1`). Автозапуск — Run-ключ `Speaches`,
firewall-правило для порта 8000. Закреплено: `av<13`, `huggingface-hub<1.0` (конфликты
только с неиспользуемыми aiortc/gradio). В `run-speaches.ps1` выставлен
`HF_HUB_OFFLINE=1`: faster-whisper при (пере)загрузке модели ходит на
huggingface.co, и при отвалившейся связи запрос падал с 500; обе модели
(whisper-1 → deepdml/faster-whisper-large-v3-turbo-ct2, tts-1 →
speaches-ai/Kokoro-82M-v1.0-ONNX) есть в кэше `~/.cache/huggingface/hub`.
Новые модели при offline-режиме не скачаются — качать вручную
`snapshot_download` или временно убрать env.
CUDA для whisper: ctranslate2 на Windows грузит только DLL из своего
package_dir (`__init__.py` → `os.add_dll_directory`), поэтому
`cublas*`/`cudnn*`/`nvrtc*` из pip-пакетов `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` скопированы в `site-packages\ctranslate2\`.
При переустановке/апгрейде ctranslate2 — повторить копирование
(источник: `site-packages\nvidia\{cublas,cudnn,cuda_nvrtc}\bin\*.dll`);
иначе STT падает 500 «Library cublas64_12.dll is not found».
На сервере провайдер `speaches-lan` (kind `openai-compatible`, base_url
`http://<win-host>:8000/v1`); capability `stt` привязана к нему как `whisper-1`
(params `{"language": "ru"}`).
Whisper на тишине/шуме выдаёт галлюцинации из субтитров («Субтитры сделал
DimaTorzok», «Редактор субтитров …», «Thanks for watching») — `media/stt.py`
`_hallucinated` отбрасывает транскрипт целиком, если все сегменты — известные
артефакты (сравнение lowercase, только буквы/цифры; списки
`_HALLUCINATION_PHRASES`/`_HALLUCINATION_PREFIXES`, warning в journald).
Пустой транскрипт → `empty_transcript` в voice-ручках и не создаёт Voice
(pipeline не вызывает `assign_voice`, чтобы шум не плодил «Голос N»).
В кэше также `Systran/faster-whisper-large-v3` (полная v3, ~2× медленнее
turbo) — в диалоге binding поле «Модель» показывает datalist из
`GET /admin/providers/{id}/models` (прокси на `{base_url}/models`
провайдера; для `stt`/`tts` пробрасывается `task=`
automatic-speech-recognition/text-to-speech — фильтр Speaches).
TODO: привязать capability `tts` → `speaches-lan`, model `tts-1` (после проверки голосов Kokoro).

## Speaker-ID

Каждое аудио-вложение (голосовые PWA/Telegram, hold-to-talk esp32home,
аудиодорожка видео) помимо транскрипта получает voice-embedding
(`media/pipeline.py` — параллельно со STT на отдельной сессии, деградация
warning-безопасная). Эмбеддинг считает соседний проект `~/projects/speakerid`
(FastAPI + SpeechBrain ECAPA-TDNN `speechbrain/spkrec-ecapa-voxceleb`,
192-dim, torch CPU): `POST /embed` (multipart wav) →
`{"embedding": [...], "dim": 192}`, `GET /healthz`. Развёртывание на
Windows-хосте по образцу Speaches: venv + `pip install .`, Run-ключ
`SpeakerId` → `speakerid.exe` (env `SPEAKERID_PORT`, дефолт 8100;
`HF_HUB_OFFLINE=1` после первого прогрева — модель ~80 МБ качается с HF),
firewall-правило 8100.

На сервере провайдер `speaker-lan`, kind `speaker-http`, base_url
`http://<win-host>:8100`; capability `speaker` привязана к нему, пороги —
в params биндинга: `{"match_threshold": 0.45, "learn_threshold": 0.60,
"min_duration_s": 1.0}` (калибровать на реальных записях). Клипы короче
`min_duration_s` не эмбеддятся.

Таблицы (миграция 0011 + `authenticated` в 0012): `voices` (label «Голос N»,
`user_id` — ручной маппинг админа, `authenticated` — считать аудио
пришедшим от этого юзера, `centroid` — L2-нормированное среднее сэмплов,
`sample_count`, `sample_attachment_id` — образец для прослушивания,
`last_heard_at`), `voice_samples` (`attachment_id` UNIQUE — повторный
прогон обновляет строку; кап 300 последних сэмплов на голос). Логика —
`media/speaker.py`: `assign_voice` матчит по центроидам косинусом;
ниже `match_threshold` создаёт новый Voice, при `>= learn_threshold`
сэмпл дообучает центроид (защита от отравления).

Результат — `derived.speaker` `{"voice_id","user_id","name","score"}`
(name = display_name замапленного юзера, иначе label), копия в text-парте
сообщения; в промпте групповой сессии имя голоса подставляется вместо
имени аккаунта-отправителя (`agent/runtime.py` `_content`). Галочка
«аутентифицировать» (`voices.authenticated`, только при замапленном
юзере) меняет это на настоящее авторство: сообщение пишется с
`author_id` владельца голоса — лента показывает его имя, а ран выполняется
с его ролью/правами. Снимок в партах при этом не слепок: PATCH
(label/маппинг), merge и перенос сэмпла переписывают `speaker` в
сохранённых `messages.parts` и `attachments.derived`
(`speaker.relabel_speaker`), а аутентификация и асинхронный пайплайн
ретаргетят `author_id` уже опубликованных сообщений
(`speaker.authenticate_attachment`); затронутым сессиям шлётся
`messages.invalidate` — открытый чат перечитывает хвост (`resyncTail`).
Снятие галочки историю не откатывает. В PWA — chip на сообщении;
`/admin` → «Голоса»: редакт. label, маппинг, галочка аутентификации,
прослушивание образца, merge голосов, управление сэмплами
(REST `/admin/voices*`, депс `manage_users`); drag&drop — сэмпл за ручку
на другой голос переносит его (`POST .../samples`, строка сэмпла сама
переезжает), голос за ручку на другой голос — merge (с confirm). Backfill по существующим
аудио — `concierge backfill-speaker [--limit N] [--conversation-id ID]`
(идемпотентен).

## Именованные модели («спроси …»)

`/admin` → Провайдеры → «Именованные модели»: имя (напр. «Алиса») → провайдер +
модель (+ опциональный системный промпт). Агент получает инструмент `ask_model`
(enum имён — из включённых строк таблицы `named_models`): «спроси Алису …»
вызывает её одноразовым запросом без истории чата. Гейтинг — `tools.allow/deny`
роли; чеклист «Инструменты» в настройках модели (`named_models.tools`, имена из
`GET /admin/named-model-tools`) даёт ей подмножество функций агента
(web_search, fetch_url и пр., кроме самого ask_model), дополнительно
пересечённое с правами вызывающей роли; ctx-инструменты работают от имени
вызывающего. Для OpenAI-совместимых провайдеров base_url — корень API (вставленный
суффикс `/chat/completions` отрезается при построении клиента, хранится как
введён). Пример Алисы: провайдер `openai-compatible`, base_url
`https://ai.api.cloud.yandex.net/v1`, api_key_ref → имя настройки/env ИЛИ сам
ключ (неразрешимый ref считается литеральным ключом), модель
`gpt://<folder_id>/aliceai-llm/latest`.

## Приглашения и пересылка (инструменты агента)

`invite_member` («Позови Тихона») — добавляет пользователя в текущую сессию
(личная становится групповой), пишет системное «… добавлен в сессию» и шлёт
`conversation.updated`/`message.created` подписчикам + DM-пинг в Telegram.
`forward_messages` («Перешли это Насте», «Перешли последние сообщения Тихону»)
— копирует сообщения перед триггером (`count` штук или хвост по `gap_minutes`)
в личную сессию пары «просящий ↔ получатель» (ищется/создаётся как group из 2
участников); текст идёт с шапкой «Переслано от …» и флагом `forwarded`,
медиа-парты получают копию attachment, привязанную к целевой сессии (доступ
к вложениям — по членству). Имена резолвятся по display_name/username
(точное, затем префикс/слово; неоднозначность → список кандидатов). Оба
инструмента — в `tools.allow/deny` роли.

То же ядро (`service.forward_to_user`) обслуживает REST:
`POST /conversations/{id}/messages/forward` (`{user_id, message_ids}`) —
для ручной пересылки из PWA: в шапке чата кнопка выбора включает режим
чекбоксов, нижняя панель даёт «Переслать» (диалог выбора получателя,
участники текущей сессии скрыты — им уже видно) и «Удалить» (цикл по
`DELETE /messages/{id}`).

## Лог сессии и компакшен

Ответы ассистента хранят полный лог прогона: `thinking` (с provider-подписями
для round-trip), `tool_call` (name/args/tool_call_id), `tool_return` (content,
`retry`/`outcome`/`native`) — сериализация/реплей в `agent/history.py`
(`serialize_parts`/`deserialize_parts`). При реплее dangling-вызовы получают
синтетический «[вызов прерван]» return, сиротские return'ы отбрасываются,
легаси `{"type":"tool"}` (без args/result) пропускаются.

Авто-компакшен: при оценке контекста ≥75% от `params.context_length` биндинга
capability `chat` старые сообщения сворачиваются в конспект (модель —
`chat_fast`, фолбэк `chat`), в ленту пишется системное сообщение
`{"type":"compaction","text","upto_seq","count"}`; история для следующих
запросов начинается с конспекта + хвост из 12 сообщений. Оценка —
`max(input_tokens прошлого run, chars/4)`. Без `context_length` в params
биндинга компакшен выключен. Задаётся в `/admin` → Провайдеры → params JSON
биндинга, напр. `{"context_length": 128000}`.

## Аналитическая БД (OLAP)

`concierge_analytics` — отдельная БД на том же кластере PG14; агент получает к
ней SQL-доступ через MCP-сервер `analytics` (таблица `mcp_servers`, stdio,
`/srv/concierge/mcp/mcp_analytics/.venv/bin/mcp-analytics`; env из setting
`mcp.env.analytics`). Сервер — свой проект `mcp_analytics` (uv + hatchling,
заменил postgres-mcp): кроме read-only `query` даёт `import_rows` и
`load_attachment` с записью в схему `sandbox`.

Синхронизация из `concierge`: НЕ logical replication — на ALT-сборке нет
`pgoutput` (проверено: rpm postgresql14/15-server его не содержит, chDB не
работает на Celeron N4100 — нет AVX). Вместо этого postgres_fdw: схема `src`
(foreign tables), `concierge_fdw_ro` — read-only роль источника, user mapping
на владельца `analytics`. Каждую минуту `concierge-analytics-sync.timer`
(`/srv/concierge/scripts/analytics-sync.sql`) прогоняет
`sync_from_src(public.t, src.t)` — upsert по PK + анти-join delete
(generated-колонки типа `messages.tsv` пропускает, считает локально).
Таблицы: conversations, users, messages, runs, conversation_members,
attachments, audit_log, memory_items, telegram_messages. FK на подписчике
сброшены — копия не проверяет ссылки.

Доступ агента: роль `analytics_ro`, trust-auth с localhost в pg_hba (только
эта БД). SELECT на runs/conversations/conversation_members/attachments/
audit_log/memory_items/telegram_messages + вьюхи `agent_messages`
(без parts/tsv) и `agent_users` (без password_hash/prompt_fragment);
запись — только схема `sandbox` (таблицы создаются/дополняются инструментами
импорта, типы колонок выводятся из значений). Инструменты: `analytics_query`
(read-only SQL, кап строк + statement_timeout), `analytics_import_rows`
(JSON-строки → `sandbox.<table>`, append/replace), `analytics_load_attachment`
(csv/tsv/json/jsonl из attachments/blob'ов → `sandbox.<table>`),
`analytics_import_star` (нормализация в звёздную схему, см. ниже). Инструкция
агенту — в глобальной настройке `agent.system_prompt` (блок про аналитику).
Гейтинг — `mcp_servers` в ролях (admin/adult: `*`, child/guest: пусто).

`analytics_import_star` — опциональная звёздная схема вместо flat-таблицы:
вызывающий перечисляет `dimensions` (категориальные колонки), для каждой
создаётся `sandbox.dim_<t>_<col>` (id IDENTITY + value UNIQUE, дедуп через
ON CONFLICT, id стабильны между append'ами), факты ложатся в
`sandbox.fact_<t>` (id + `<col>_id` + меры/passthrough), а
`sandbox.v_<t>` — flat-view с LEFT JOIN'ами обратно (читать её, как обычную
таблицу). `measures=None` → все numeric-колонки. NULL в dim-колонке →
NULL dim_id. Маппинг dim-колонок пишется в `sandbox._dims_registry`.
Конкурентные вызовы сериализуются advisory-lock'ом на схему. Без
авто-детекции измерений, SCD и FK на dim_id.

## Удалённые сессии

`conversations.status` — `active | archived | deleted` (soft-delete, миграция
0010). `DELETE /api/conversations/{id}` только для архивных: нужен пермишен
`delete_conversations` (в ролях; у admin через `*`) + owner/`*`. Удалённые
скрыты для всех, кроме `*`: не отдаются в `GET /conversations` (фильтр
`?status=deleted` и `all` — только админу), `_member_conv_or_404` отдаёт 404,
поиск/retrieval и attachments недоступны. В PWA — таб «Удалённые» у админа;
админ может вернуть сессию в архив (PATCH status=archived).

## Админка и эксплуатация

`/admin` в PWA (иконка-гаечный ключ в шапке, видна при `manage_*`/`settings.global` правах):
пользователи/роли/права, редактор промпта с предпросмотром, провайдеры и bindings, MCP,
code-agents, Telegram, журнал runs/затрат и аудит-лог. Аудит пишется в таблицу
`audit_log` (миграция 0006); метрики — `GET /api/metrics` (admin).

Бэкапы на сервере: `concierge-backup.timer` (ежедневно 04:00) → `/srv/concierge/backups/<дата>/`
(`pg_dump -Fc` + rsync blobs + per-collection снапшоты Qdrant; ротация 14 дней).
Ручной запуск: `systemctl start concierge-backup`. Скачать локально:
`deploy/backup-pull.sh` → `$CONCIERGE_BACKUP_DIR`. Восстановление:
`ssh "$CONCIERGE_REMOTE" 'bash /srv/concierge/app/deploy/restore.sh <YYYY-MM-DD>'` —
останавливает сервис, пересоздаёт БД, возвращает blobs и снапшоты Qdrant.
Логи — journald, лимит 200M/1 месяц (`/etc/systemd/journald.conf.d/concierge.conf`).

## Telegram-бот

Включается из PWA (`/admin`, раздел Telegram): `telegram.enabled` + `telegram.bot_token`
(секрет — нужен `CONCIERGE_SECRET_KEY`); супервизор подхватывает изменения без рестарта
(~10 с). Привязка аккаунта: код в `/settings` → `/start <код>` боту. Чтобы бот видел все
сообщения групп/топиков (они сохраняются в групповую сессию, ответ — только по
@упоминанию/reply), отключите privacy mode у BotFather (`/setprivacy`) или сделайте бота
админом группы.

## Режим ответа (reply_mode)

`conversations.reply_mode` — `always` (личные по умолчанию) или `mention`
(групповые по умолчанию; PATCH `/conversations/{id}`, в PWA — панель деталей).
В `mention` ран запускается только если `prompt.is_mentioned` находит голое
`{"type":"mention"}` (без user_id) или токен (`@консьерж`, `@assistant`,
`@concierge`, `@{agent.name}`) в тексте. Для транскрибированных голосовых
партов (`"transcribed": true` — колонка esp32home, hold-to-talk, голосовые в
Telegram) токены матчатся без `@`: фраза «Консьерж, …» триггерит, обычная
речь — нет (сообщение всё равно сохраняется и распознаётся). Сессия
«Гостиная» колонки — в `mention`.

## Голосовая линия (voice_session)

`conversations.settings.voice_session: true` помечает сессию-колонку
(«Гостиная», id=27): если она ушла в архив/удалена, `POST .../voice-message`
не падает — `service.resolve_voice_conversation` возвращает активного
наследника линии (`settings.voice_root` = id корня) или клонирует последнюю
сессию (kind/title/reply_mode/origin, участники — ВСЕ пользователи, settings
+ `voice_root`). Колонка шлёт на тот же CONV_ID — сервер сам резолвит
актуальную сессию; в ответе `conversation_id` — куда реально попало
сообщение. Без флага: archived → 409, deleted → 404 как раньше.

## Qdrant — решение по доступу

Qdrant на сервере слушает `0.0.0.0:6333/6334` — сознательно открыт на LAN:
им пользуются другие локальные сервисы (напр. `hermes_*` коллекции). Закрывать порты нельзя;
политику пересматривать только вместе с миграцией этих сервисов.
