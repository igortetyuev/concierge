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
curl -k https://$CONCIERGE_DOMAIN/api/healthz   # -> {"db":"ok","qdrant":"ok"}
```

Web-root (`$CONCIERGE_DEST/web`) чистится `rsync --delete`. Каталог верхнего
уровня с файлом-меткой `.keep` считается чужим и исключается из деплоя —
сторонние статичные подсайты кладутся туда же; им отдаётся `<dir>/index.html`
(`try_files` пробует `{path}/index.html` до SPA-fallback).

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
Автозапуск на Windows — задача планировщика `Concierge-Ollama` →
`C:\Users\<user>\ollama-home\run-ollama.ps1` (`OLLAMA_HOST=0.0.0.0`,
`ollama.exe serve`, лог `ollama.log`; трей-приложение `ollama app.exe` не
используется — автообновлений нет), firewall-правило для порта 11434.
LAN-сервисы (Ollama, Speaches, SpeakerId, KokoroTts) — задачи `Concierge-*`:
триггер «при запуске системы», principal igor с LogonType S4U (без пароля,
без интерактивного входа; процессы в session 0, CUDA там работает),
ExecutionTimeLimit 0, рестарт каждую минуту. Раньше были HKCU Run-ключи —
после перезагрузки с входом другого пользователя (tikhon) сервисы не
стартовали и concierge сыпал `httpx.ConnectError` (бэкап ключей —
`C:\Users\<user>\run-keys-backup.reg`). Управление:
`Get-ScheduledTask Concierge-*`, `Start-/Stop-ScheduledTask`.
На сервере заведён провайдер `ollama-lan` (kind `ollama`, тот же base_url); capability `vision`
привязана к нему как `qwen3-vl:4b-instruct` (инференс на GPU).

## STT/TTS

Speaches 0.8.3 на том же Windows-хосте (venv `C:\Users\<user>\speaches-venv`,
cwd `C:\Users\<user>\speaches-home`, лог `speaches.log`):
`http://<win-host>:8000/v1`, whisper `large-v3-turbo` на GPU
(`WHISPER__INFERENCE_DEVICE=cuda` в `run-speaches.ps1`, GTX 1070;
~1 с на короткий клип против ~11 с на CPU; алиас `whisper-1`),
Kokoro TTS (`tts-1`). Автозапуск — задача `Concierge-Speaches` (см. Эмбеддинги),
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

### KokoroTTS (синтез речи, ru+en)

Сайдкар `kokorotts` (репо `~/projects/kokorotts`, на хосте —
`C:\Users\<user>\kokorotts`, venv `kokorotts-venv`, cwd `kokorotts-home`,
лог `kokorotts.log`): `http://<win-host>:8200/v1`, OpenAI-совместимый
`POST /v1/audio/speech` → 24 kHz WAV. Русский — `zaakirio/kokoro-ru`
(KModel + RUAccent g2p, голоса `sveta`/`masha` на base-чекпоинте,
`dima` на `kokoro-ru-v2-dima.pth`), английский — stock Kokoro-82M
(`af_*`/`am_*`/`bf_*`/`bm_*`, дефолт `af_heart`); `voice=auto` выбирает
по кириллице. `GET /v1/audio/voices` — список голосов (проксируется
сервером как `GET /admin/providers/{id}/voices` и показывается
выпадающим списком «Голос» в диалоге tts-binding, пишется в
`params.voice`). Инференс на CPU (torch `…+cpu`, ~10x realtime, ~2.4 ГБ
RAM на ru-модель). Автозапуск — задача `Concierge-KokoroTts` по образцу
остальных, firewall `KokoroTTS 8200`. В `run-kokorotts.ps1`:
`HF_HUB_OFFLINE=1` + `PYTHONUTF8=1` (модели уже в кэше; новые —
`snapshot_download` вручную без env). `espeakng_loader` несёт свой
`libespeak-ng.dll`, системный eSpeak не нужен.
Провайдер `kokoro-lan` (kind `openai-compatible`, base_url
`http://<win-host>:8200/v1`), binding `tts` → model `kokoro`.

Важно (2026-11-23): что-то потёрло деревья в `AppData\Local\Programs` —
у `Python312` пропали `Lib/` и часть `DLLs/` (venv'ы не стартовали,
«No module named 'encodings'»), у `Ollama` — весь `lib/` с
`llama-server.exe` (embeddings/vision → 500, сам `ollama.exe` отвечал).
Восстановлено: Python — из NuGet-пакета `python` 3.12.10 (распаковка
оставлена в `C:\Users\igor\py312-nuget`), Ollama — полной перезаливкой
`ollama-windows-amd64.zip` релиза 0.34.3 поверх install-дира с
перезапуском задачи `Concierge-Ollama` (модели в `~/.ollama` не
тронуты). Все venv'ы и сервисы снова рабочие — после перезагрузки
поднимутся. Если `Lib`/`lib` снова исчезнут — искать, что их трёт
(антивирус/«клинер»?).

TTS-ответы: автоголос (`voice_quick` PWA, `voice_session` «Гостиная»,
Telegram `voice_reply`) озвучивает `tts.spoken_version` — текст чистится
от markdown и при >600 зн. сжимается `chat_fast` до 1–2 фраз (полный
текст остаётся в ленте, аудио — аттачментом `derived.spoken`).
`voice_session` дополнительно пушит `/api/attachments/{id}` колонке
esp32home (`POST {SPEAKER_URL}/play`, токен `SPEAKER_TOKEN` из
`mcp.env.livingroom`), ошибки только логируются. Кнопка «проговорить»
на любом сообщении ассистента (`POST
/conversations/{cid}/messages/{mid}/speak`) озвучивает текст целиком.

## Speaker-ID

Каждое аудио-вложение (голосовые PWA/Telegram, hold-to-talk esp32home,
аудиодорожка видео) помимо транскрипта получает voice-embedding
(`media/pipeline.py` — параллельно со STT на отдельной сессии, деградация
warning-безопасная). Эмбеддинг считает соседний проект `~/projects/speakerid`
(FastAPI + SpeechBrain ECAPA-TDNN `speechbrain/spkrec-ecapa-voxceleb`,
192-dim, torch CPU): `POST /embed` (multipart wav) →
`{"embedding": [...], "dim": 192}`, `GET /healthz`. Развёртывание на
Windows-хосте по образцу Speaches: venv + `pip install .`, задача
`Concierge-SpeakerId` → `run-speakerid.ps1` → `speakerid.exe` (env `SPEAKERID_PORT`, дефолт 8100;
`HF_HUB_OFFLINE=1` после первого прогрева — модель ~80 МБ качается с HF),
firewall-правило 8100.

На сервере провайдер `speaker-lan`, kind `speaker-http`, base_url
`http://<win-host>:8100`; capability `speaker` привязана к нему, пороги —
в params биндинга: `{"match_threshold": 0.45, "learn_threshold": 0.60,
"min_duration_s": 1.0}` (калибровать на реальных записях). Клипы короче
`min_duration_s` не эмбеддятся.

Speaker-ID запускается только там, где аккаунт не идентифицирует
говорящего (`media/speaker.py` `speaker_id_allowed`): permission
`speaker_id` в `role.permissions` (аккаунт `speaker`, роль `speaker` —
шаред-устройства) ИЛИ `derived.origin == "telegram"` у аттачмента (TG
проставляет его в `create_attachment`; загрузчик — привязанный аккаунт,
голос может быть чужой). Аудио, загруженное личным аккаунтом из PWA,
speaker-ID не проходит: отправитель известен, эмбеддинг не запрашивается,
сэмпл не сохраняется, chip не показывается — сообщение авторствует
загрузившим юзером. Причина: короткие PWA-клипы (~1.5 с webm) дают
разбросанные эмбеддинги (косинус к центроиду 0.2–0.5 при пороге 0.45) и
плодили «Голос N»-сирот, а понизить порог нельзя — центроиды Игорь↔Тихон
на косинусе 0.47. `embed_attachment` (админка, backfill) проверяет
допуск на uploader'е аттачмента, не на вызывающем.

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
(`speaker.relabel_speaker`), а аутентификация, асинхронный пайплайн и
перенос сэмпла в аутентифицированный голос ретаргетят `author_id` уже
опубликованных сообщений (`speaker.authenticate_attachment` — матчит
оба ключа партов, `attachment_id` и `voice_attachment_id`); затронутым
сессиям шлётся `messages.invalidate` — открытый чат перечитывает хвост
(`resyncTail`). Удаление голоса снимает `speaker`-слепки с его
аттачментов и сообщений (`speaker.clear_speaker`).
Снятие галочки историю не откатывает. В PWA — chip на сообщении;
`/admin` → «Голоса»: редакт. label, маппинг, галочка аутентификации,
прослушивание образца, merge голосов, управление сэмплами
(REST `/admin/voices*`, депс `manage_users`); drag&drop — сэмпл за ручку
на другой голос переносит его (`POST .../samples`, строка сэмпла сама
переезжает), голос за ручку на другой голос — merge (с confirm). Backfill по существующим
аудио — `concierge backfill-speaker [--limit N] [--conversation-id ID]`
(идемпотентен).

Авторство в промпте: system prompt каждого рана всегда содержит
`[Пользователь: <display_name>]` автора trigger-сообщения, даже без
`prompt_fragment` (`agent/prompt.py`). В группах каждое user-сообщение
дополнительно рендерится с префиксом `<display_name>: …` (для
распознанного голоса — имя голоса/владельца).

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

## Планировщик (события и напоминания)

Таблица `scheduled_jobs` (миграция 0013) + фоновый воркер
`schedule/worker.py` в lifespan (`api/app.py`), образец — memory indexer:
спит до ближайшего `next_run_at` (макс 60 с), `kick()` после любых
изменений. Один uvicorn-процесс — воркер in-process.

Паттерн — `schedule/recurrence.py` `ScheduleSpec` (структурный, не RRULE):
однократно `at`/`delay_minutes` или `freq` (minutely|hourly|daily|weekly|
monthly|yearly) + `interval`, `weekdays`, `times` ("HH:MM"), `month_days`
(-1 = последнее), `start/until/count`, `dates` (доп. срабатывания),
`exclude_dates`. Расчёт — dateutil rruleset над naive-local временем
семьи (`get_settings().timezone`), в БД — naive UTC.

Срабатывание (`schedule/service.py` `fire`): user-authored сообщение
`source_channel="schedule"`, парт `{"type":"text", text, scheduled:{...}}`,
`client_msg_id="sched:{id}:{occurrence}"` (идемпотентность), обычный ран
агента от имени владельца (`wait=True` — ждёт lock, не пропускается;
триггер считается упоминанием → будит `reply_mode=mention`). В PWA такие
сообщения — системная строка «⏰ «title»: text», TG-sink их не зеркалит.
Правила сессии: `command` с мин. интервалом ≥24 ч (или однократная) →
новая личная сессия на каждое срабатывание (`origin="schedule"`,
`settings.schedule_job_id`); чаще → переиспользуется `job.conversation_id`
(новая при первом срабатывании/архиве/удалении). `reminder` → сессия,
где попросили: archived → восстанавливается в active, deleted/не
участник → новая личная. Claim: `next_run_at` сдвигается и коммитится до
запуска; пропуски >12 ч пропускаются (warning), ≤12 ч — одно позднее
срабатывание; исчерпанные (однократные/until/count) удаляются. Пауза —
`paused_until`, срабатывания внутри пропускаются; отмена = DELETE строки.
Лимит 100 паттернов на пользователя.

Инструменты агента (`schedule/tools.py`, в `CORE_TOOLS`, гейтинг
`tools.allow/deny`): `schedule_task` (kind command|reminder),
`list_scheduled_tasks`, `pause/resume/cancel_scheduled_task` — только
свои. REST (`api/schedule.py`, только свои): `GET /schedules`,
`PATCH /schedules/{id}` `{paused_until: iso|null}` (null = возобновить),
`DELETE /schedules/{id}` → `{"ok": true}`. PWA — `/settings` секция
«Расписание»: список, пауза до даты, возобновить, удалить, ссылка на
сессию. Внешних уведомлений (TG DM, push) нет.

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

## Скиллы (skills/)

Скилл — текст SKILL.md (agentskills.io: YAML-шапка `name`/`description` +
Markdown-тело, формат Claude/Devin/Hermes), хранится в `skills`
(миграция 0015). `owner_id IS NULL` — общий (`manage_skills`, `/admin` →
«Скиллы», чекбоксы ролей → `role_ids`, пусто = все роли); `owner_id = user` —
личный (`create_skills`, `/settings` → «Мои скиллы» или инструментом
`save_skill`), виден только владельцу и перекрывает общий с тем же именем.
Полный текст — источник истины; `name`/`description`/`aliases`/`triggers`/
`argument-hint` денормализованы в колонки, неизвестные ключи шапки
(`allowed-tools`, `metadata`…) сохраняются, но ни на что не влияют.
Расширения шапки: `aliases` — кириллические /команды (`[\w-]`), `triggers` —
фразы запуска (нормализация: lower, ё→е, целыми словами; ≥2 слов или ≥6
символов), `ephemeral: true` — разовый скилл-справка: тело действует только
на ран вызова (`active_skill` не пишется, `skill_event` не постится,
снимать нечего; чанк `[Скилл: name — на этот ответ]`). Зарезервированы:
stop, skills, start, new, sessions, archive, voice, help. Файлы скилла — `skill_files` (только тексты ≤256 КБ, ≤30 шт.,
скрипты не исполняются), читаются инструментом `read_skill_file`.

Запуск в той сессии, где вызван (`skills/service.py` activate →
`conv.settings["active_skill"]`, перечитывается на каждый ран; старт/конец —
системный парт `skill_event`):
- `/имя аргументы` — PWA, Telegram (в т.ч. группы: неизвестная команда
  становится обычным сообщением, если `resolve()` нашёл скилл) и голос;
- фраза-триггер — только если в сессии нет активного скилла;
- модель — `use_skill` (каталог `[Скиллы]` в промпте, если инструмент разрешён).
Снятие — `end_skill`, `/stop` (не создаёт Run), `DELETE /conversations/{cid}/skill`
или крестик chip в шапке; удалённый/выключенный скилл снимается молча.
Команда или интерактивный ответ (кнопка `choice`, сабмит чек-листа),
пришедшие под локом рана, переочередиваются с `wait=True`
(`runtime.maybe_run` → `_must_requeue`). `/имя` и `/stop` считаются
обращением в `reply_mode=mention`.
Подстановки в теле: `$ARGUMENTS`, `$ARGUMENTS[N]`, `$N`, `${CLAUDE_SKILL_DIR}`
→ `skill:<name>`; нет плейсхолдера — дописывается `ARGUMENTS: …`.
Перед телом — `SKILL_COMPAT_PREAMBLE` (`skills/activation.py`): AskUserQuestion/
clarify → `ask_user_question`, Task/delegate_task → `run_subagent` (пачка =
несколько вызовов), Read/`skill_view`/`${CLAUDE_SKILL_DIR}` → `read_skill_file`,
Bash/Edit/Write недоступны (правки кода — `delegate_coding_task`).

Новые инструменты в `DYNAMIC_TOOLS` (не в `NAMED_MODEL_TOOLS`), гейтинг —
`tools.allow/deny` роли; `save_skill` дополнительно требует `create_skills`.
Роли с явным `tools.allow` новые имена не получают автоматически — включать
вручную.

`ask_user_question` — вопрос с вариантами (1–4 вопроса, ≤4 опций + «Другое»,
`multi_select`). Инструмент кладёт парт `choice` в ответ и просит модель
завершить ход; в PWA — карточка с кнопками, в TG — inline-клавиатура (callback
`ch:{message_id}:{idx}`, только single-question single-select) либо нумерованный
текст. Ответ — `POST …/messages/{id}/answer` → `skills/choices.py`
`answer_choice`: user-сообщение с `choice_answer` + `message.updated` на исходное
(`answered: true`) + новый ран. Свободный текст — тоже ответ. Ответ считается
mention в `reply_mode=mention` и пропускает фразы-триггеры; на парт записывается
`skill`/`skill_id` показавшего вопрос скилла — при ране ответа скилл резолвится
по id среди скиллов ответившего (`_answer_skill`).
`run_subagent(goal, context, model=default|fast)` — вложенный Agent без истории
(binding chat / chat_fast, токены в `ctx.usage`), инструменты = права роли минус
`SUBAGENT_EXCLUDED` (чат-мутации, рекурсия, планировщик), глубина 1, до 5
параллельно на ран, таймаут 300 с, ответ ≤8000 зн.

REST: `GET /skills` (доступные), `/skills/mine` CRUD + `/import` + `/{id}/export`
+ `/files/{path}`, `/admin/skills` — то же с `role_ids`/`enabled`.
Импорт `.md`/`.zip` (SKILL.md в корне или одной папке; не-UTF8 пропускается
с отчётом), экспорт — `<name>/…`. Лимиты: SKILL.md ≤64 КБ, ≤100 личных,
каталог ≤50 скиллов в промпте.

## Чек-листы (show_checklist)

`show_checklist(items, title, columns, submit_label)` — динамический
инструмент (`agent/tools.py`, в `DYNAMIC_TOOLS`, гейтинг `tools.allow`
роли): интерактивный список с галочками, 1–50 пунктов, один чек-лист на
ран, `id` пункта по умолчанию "1".."n" (уникальные, ≤64). Два режима:
list (`columns=None` — только `item.text`) и table (`columns` ≤6 +
`item.cells` на каждый пункт; чекбокс — первой колонкой, ячейки
рендерятся markdown). Не регистрируется в голосовых сессиях
(`voice_quick`/`voice_session` — UI нет). Инструмент кладёт в ответное
сообщение парт `{"type":"checklist", checklist_id, title, columns, items,
submit_label, skill, skill_id, submissions, submitted_by, submitted_at}`;
`skill`/`skill_id` — скилл, показавший лист (нужен для re-apply на ответе).

Отправка: PWA — `ChecklistPart.tsx` без кнопки «Отправить»: каждое
переключение галочки авто-сабмитит ПОЛНОЕ состояние с дебаунсом 600мс
(серия кликов схлопывается в один submit; «Отправлено ×N» — счётчик
серверных submissions). Модель синхронизации: серверное состояние парта —
источник истины, приходит во все окна/устройства realtime-событием
`message.updated`; локальные клики — optimistic-дельты (`pending`),
накладываемые поверх него и снимаемые, когда эхо подтверждает значение —
поэтому позднее эхо прошлого submit'а не мерцает и не затирает свежий
клик, а правки с разных устройств мерджатся (конфликт на одном пункте —
last writer wins). Submit'ы выстраиваются в цепочку (нет переупорядочивания
на сервере), net-zero серия без in-flight запросов не шлётся вовсе,
при unmount компонента pending-submit сбрасывается немедленно →
`POST /conversations/{cid}/messages/{mid}/checklist` `{checklist_id,
checked:[ids]}` (201, переотправка разрешена; повторная отправка того же
состояния дедупится — возвращает прошлый ответ, нового рана нет). Сабмиты
принимаются только у сообщений ассистента и только в активной сессии —
и в PWA, и в TG (`_checklist_part`). Telegram — `_send_checklist`
шлёт нумерованный список (только `item.text`, cells не показываются;
длинный список режется на чанки, клавиатура — на последнем) +
клавиатуру `checklist_markup`: toggle `cl:{mid}:{idx}:{maskhex}` (бит i =
item i checked; только edit_markup, без рана) и submit `cs:{mid}:{maskhex}`;
после сабмита клавиатура перерисовывается по сохранённому состоянию.
Общий код — `skills/checklists.py submit_checklist`: блокирует сообщение
FOR UPDATE, обновляет парт на
месте (checked, ++submissions, submitted_by/at → `message.updated`),
постит user-сообщение «Отмечено в «title»: ☑ …/☐ …» с полем
`checklist_answer` `{message_id, checklist_id, skill, skill_id,
items:[{id,checked}], changed:[{id,checked}]}` (dedup
`cl:{mid}:{submissions}:{checklist_id[:8]}`) и планирует ран. `changed` —
diff против сохранённого состояния (первый submit — против начального).
Такое user-сообщение и ответ ассистента на него — служебные
(`messages.hidden`, миграция 0017): остаются в БД и в истории для агента,
но не отдаются REST-лентой, не пересылаются и не рассылаются
realtime-событиями. Вместо бабблов переписка складывается в
`followups` самого checklist-парта (блок «рассуждение» карточки в PWA):
submit добавляет туда pending-запись `{n, answer, at, parts: null}`
(`n` = номер submission, дублируется в `checklist_answer`), а runtime по
завершении скрытого рана заполняет `parts` компактной копией ответа
(`compact_followup_parts` — text/thinking verbatim, tool_call → чип
без args/returns) и шлёт `message.updated`. Поиск поэтому находит
текст «Отмечено в…» в видимом сообщении чек-листа, но не hidden-строки.

Ран по ответу чек-листа — интерактивный reply: считается mention
(`_is_interactive_reply`), пропускает фразы-триггеры, ephemeral-скилл с
парта re-apply (`_answer_skill`) — резолвится по `skill_id` среди скиллов
ОТВЕТИВШЕГО пользователя (имя — фолбэк для старых партов), а не того, кто
показал лист. В промпт попадает строка
`[чек-лист: {"items":{"id":bool,…},"changed":["id",…]}]` — changed это
id'шники, новое состояние читается из items.

Применение — домашка: ephemeral-скилл `school` показывает
`sandbox.school_homework` чек-листом; у таблицы нет стабильных id
(push.py пересобирает её TRUNCATE+INSERT), поэтому `id` пункта =
`hw_id`, вычисляется в SQL как
`left(md5(date::text||'|'||coalesce(subject,'')||'|'||coalesce(text,'')),10)`
и копируется; дубли (одинаковые date+subject+text) дедупятся
`DISTINCT ON (hw_id)`. Ответы складываются append-only в
`sandbox.school_homework_done(hw_id, date, subject, done, marked_by,
marked_at)` через `analytics_import_rows`: таблица заранее не создана —
первый append создаёт её с угаданными типами (date/marked_at текст →
строгие ISO-форматы, сортировка лексикографическая); актуальное состояние
— самая свежая строка по marked_at. Правка текста задания учителем меняет
hw_id → отметка теряется.

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
С мака тот же сервер доступен как stdio через SSH — Devin MCP `analytics`
в `.devin/mcp_config.local.json`: `ssh box env HOME=… DATABASE_URL=…
ANALYTICS_BLOB_DIR=… …/mcp-analytics` (значения env — те же, что в setting
`mcp.env.analytics`; trust-auth `analytics_ro` работает, т.к. процесс на хосте).

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
агенту — общий скилл `analytics` (ephemeral: справка на один ран, каталог +
триггеры/«аналитика»).
Гейтинг — `mcp_servers` в ролях (admin/adult/speaker: `*`,
child: `["gismeteo", "livingroom", "analytics"]`, guest: `["livingroom"]`).

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

Школьные данные (`sandbox.school_*`) — источник не concierge, а проект
`~/projects/myschool` на маке: после каждого обновления из приложения
«Моя школа» `.claude/skills/analytics/push.py` делает full refresh по ssh
(`analytics_ro`, psql на localhost). `school_schedule` (dow, lesson,
start_time, subject_odd/even — чередование по чётности ISO-недели),
`school_homework` (date — на какой день задано, text, materials jsonb),
`school_grades` (mark/value/weight/work_type/…, class_stats jsonb),
`school_quarters`, `school_meta` (generated_at/loaded_at — свежесть).
Отдельно `school_homework_done` — выгружается не myschool, а пишется самим
консьержем (append-only отметки «домашка сделана» из чек-листа, см.
«Чек-листы»). Схема описана агенту в общем скилле `school` (ephemeral,
триггеры «домашка»/«оценки»/… и /школа).

## MCP-серверы с браузерным доступом (ozon, browserdata)

`ozon` — HTTP на макбук: transport `http`,
`http://macbook-2.local:8399/mcp/`. На маке это launchd-агент
`local.mcp-ozon` (`~/Library/LaunchAgents/local.mcp-ozon.plist`,
`MCP_OZON_TRANSPORT=http`, host 0.0.0.0, порт 8399, KeepAlive) над проектом
`~/projects/mcp_ozon` — держит постоянный ws к Chrome inspect-mode. Промпт
«Разрешить удаленную отладку» (на каждый новый ws) прокликивает второй
агент `local.mcp-ozon-allow` — watchdog из osascript
(`scripts/allow_debugging_watchdog.applescript`; osascript как executable
джоба = правильная TCC-идентичность для Accessibility, у дочерних процессов
сервиса её нет). Работает только пока мак в сети и не спит. Если Ozon
отвечает антиботом (403 + challenge HTML), воркер-таб надо прогнать
навигацией по ozon.ru — JS-челлендж сам пройдёт и API разблокируется.
Запасной вариант (сейчас не используется): локальный запуск на box,
`/srv/concierge/mcp/mcp_ozon` + Chromium с Mac-фингерпринтом — признан
слишком хрупким (GPU-zygote падает при запуске не из десктоп-сессии,
скринсейвер/окна на xrdp), детали — в `mcp_ozon/CLAUDE.md` «Deployment on
box».

`browserdata` — stdio через SSH на макбук:
`ssh -o BatchMode=yes mac /Users/igor/projects/mcp_browserdata/.venv/bin/mcp-browserdata`
(алиас `mac` = macbook-2.local в `~/.ssh/config` пользователя igor, ключ
`~/.ssh/concierge_mac`; тот же паттерн, что доступ к `analytics` с мака,
только наоборот). Живёт на маке: нужны файлы профилей и keychain. Работает
только пока мак в сети и не спит. Отдаёт пароли в открытом виде — гейтинг
только `mcp_servers` в ролях.

Недоступный MCP-сервер не роняет ран: `agent/mcp.py` `TolerantToolset`
ловит ошибку подключения, предупреждает в journald и запускает агента без
инструментов этого сервера (`init_timeout` — 15 с, у pydantic-ai дефолт 5 —
мало для stdio-через-ssh на просыпающемся маке).

## Playwright (браузер агента)

`playwright` — `@playwright/mcp@0.0.85` на box:
`/srv/concierge/mcp/mcp_playwright` (npm-проект, бинарь
`node_modules/.bin/playwright-mcp`), флаги `--browser chromium --headless
--output-dir /srv/concierge/mcp/mcp_playwright/output`. Браузер — bundled
chromium-headless-shell в `~/.cache/ms-playwright` (ставится
`node_modules/.bin/playwright install chromium`; системный /bin/chromium не
используется). Профиль persistent (логины/cookies переживают прогоны;
параллельные прогоны с тем же профилем конфликтуют — chromium singleton),
снапшоты и загрузки пишутся в output-dir. При апгрейде версии — заново
`npm i @playwright/mcp@X` + `playwright install chromium`.

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
речь — нет (сообщение всё равно сохраняется и распознаётся). Ответы на
интерактивные парты (кнопка `ask_user_question`, сабмит чек-листа) тоже
считаются mention. Сессия
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
