# Concierge

Семейный агент-консьерж. Монорепо: `server/` — FastAPI + PydanticAI + PostgreSQL + Qdrant
(деплоится на `box`, ALT Linux), `web/` — адаптивное PWA (React + Vite + TS), `deploy/` —
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

## Деплой на box

```bash
deploy/provision.sh   # одноразовая инициализация (PostgreSQL, Caddy, uv, systemd)
deploy/deploy.sh      # rsync -> uv sync -> alembic upgrade head -> restart
curl -k https://box/healthz   # -> {"db":"ok","qdrant":"ok"}
```

Конфигурация сервера — через env с префиксом `CONCIERGE_` (на box: `/etc/concierge/.env`).

## Эмбеддинги

Ollama на `winhost` (192.168.99.66, Windows): `http://192.168.99.66:11434/v1`, модель
`bge-m3` (1024-dim). Для capability `embedding` — provider kind `ollama` с этим base_url.
Автозапуск — Run-ключ `Ollama`, `OLLAMA_HOST=0.0.0.0` в user env, firewall-правило «Ollama 11434`.
На box заведён провайдер `ollama-winhost` (kind `ollama`, тот же base_url); capability `vision`
привязана к нему как `qwen3-vl:4b-instruct` (CPU-инференс ~10–20 с на картинку).

## STT/TTS

Speaches 0.8.3 на `winhost` (`C:\Users\user\speaches-venv`, cwd `C:\Users\user\speaches-home`,
лог `speaches.log`): `http://192.168.99.66:8000/v1`, whisper `large-v3-turbo` на CPU
(алиас `whisper-1`), Kokoro TTS (`tts-1`). Автозапуск — Run-ключ `Speaches`,
firewall-правило «Speaches 8000». Закреплено: `av<13`, `huggingface-hub<1.0` (конфликты
только с неиспользуемыми aiortc/gradio).
TODO: привязать capability `stt` → provider `openai-compatible` base_url
`http://192.168.99.66:8000/v1`, model `whisper-1` (и `tts` → model `tts-1`, после проверки голосов Kokoro).

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

## Админка и эксплуатация

`/admin` в PWA (иконка-гаечный ключ в шапке, видна при `manage_*`/`settings.global` правах):
пользователи/роли/права, редактор промпта с предпросмотром, провайдеры и bindings, MCP,
code-agents, Telegram, журнал runs/затрат и аудит-лог. Аудит пишется в таблицу
`audit_log` (миграция 0006); метрики — `GET /api/metrics` (admin).

Бэкапы на box: `concierge-backup.timer` (ежедневно 04:00) → `/srv/concierge/backups/<дата>/`
(`pg_dump -Fc` + rsync blobs + per-collection снапшоты Qdrant; ротация 14 дней).
Ручной запуск: `systemctl start concierge-backup`. Скачать на Mac:
`deploy/backup-pull.sh` → `~/concierge-backups`. Восстановление:
`ssh box 'bash /srv/concierge/app/deploy/restore.sh <YYYY-MM-DD>'` — останавливает сервис,
пересоздаёт БД, возвращает blobs и снапшоты Qdrant. Логи — journald, лимит 200M/1 месяц
(`/etc/systemd/journald.conf.d/concierge.conf`).

## Telegram-бот

Включается из PWA (`/admin`, раздел Telegram): `telegram.enabled` + `telegram.bot_token`
(секрет — нужен `CONCIERGE_SECRET_KEY`); супервизор подхватывает изменения без рестарта
(~10 с). Привязка аккаунта: код в `/settings` → `/start <код>` боту. Чтобы бот видел все
сообщения групп/топиков (они сохраняются в групповую сессию, ответ — только по
@упоминанию/reply), отключите privacy mode у BotFather (`/setprivacy`) или сделайте бота
админом группы.

## Qdrant — решение по доступу

Qdrant на box слушает `0.0.0.0:6333/6334` — сознательно открыт на домашнюю LAN:
им пользуется Hermes с Mac (коллекции `hermes_memories` и др.). Закрывать порты нельзя;
политику пересматривать только вместе с миграцией Hermes.
