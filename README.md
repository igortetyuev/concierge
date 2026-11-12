# Concierge

Семейный агент-консьерж: чат с ИИ для нескольких членов семьи через PWA и Telegram-бота,
с личной и общей памятью, мультимодальным вводом и ролевой админкой.

Монорепо:

- `server/` — FastAPI + PydanticAI + PostgreSQL + Qdrant (Python 3.12+, `uv`)
- `web/` — адаптивное PWA: React + Vite + TypeScript (`pnpm`)
- `deploy/` — systemd-юниты, Caddyfile, скрипты деплоя по SSH/rsync

## Возможности

- Пользователи и роли с гранулярными правами, приглашения по одноразовым ссылкам,
  refresh-токены на устройство (httpOnly cookie) + короткие JWT.
- Личные и групповые сессии (несколько людей + ИИ), общий и личный контекст памяти
  в Qdrant (инструменты `remember`/`recall`), фоновая индексация сообщений.
- Мультимодальный ввод: аудио, видео, изображения, документы — единый media-пайплайн
  (STT/vision/TTS через настраиваемые провайдеры).
- Telegram-бот (aiogram, long polling): DM = личные сессии, топики супергруппы =
  групповые сессии, привязка аккаунта по одноразовому коду.
- Именованные модели («спроси Алису …») — провайдер + модель + системный промпт
  настраиваются в админке; MCP-серверы; делегирование кода CLI-агентам по SSH.
- Генерация документов (docx/xlsx/pptx/pdf), веб-поиск и fetch_url как инструменты.
- Авто-компакшен контекста по достижении порога от `context_length` биндинга.
- Админка `/admin`: пользователи/роли/права, промпт с предпросмотром, провайдеры
  и capability-bindings, журнал runs/затрат, аудит-лог, метрики.
- Эксплуатация: ежедневные бэкапы (pg_dump + blobs + снапшоты Qdrant, ротация 14 дней),
  восстановление одной командой, логи в journald.

## Разработка

```bash
# Сервер (нужен PostgreSQL и Qdrant; тесты работают на sqlite)
cd server
uv sync
uv run ruff check .
uv run mypy src
uv run pytest

# Запуск (конфигурация — env с префиксом CONCIERGE_, см. server/.env.example)
CONCIERGE_DATABASE_URL=postgresql+asyncpg://concierge@127.0.0.1:5432/concierge \
CONCIERGE_SECRET_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
CONCIERGE_JWT_SECRET=dev-secret \
uv run uvicorn concierge.api.app:app --reload

# Первый админ
uv run alembic upgrade head
uv run concierge bootstrap-admin

# Веб
cd web
pnpm install
pnpm dev      # /api проксируется на VITE_API_TARGET (по умолчанию http://localhost:8000)
pnpm lint && pnpm build
```

`CONCIERGE_SECRET_KEY` — Fernet-ключ для шифрования секретов в БД (токен Telegram-бота,
api_key провайдеров и т.п.); потеря ключа = потеря всех сохранённых секретов.
`CONCIERGE_JWT_SECRET` обязателен в проде — дефолт только для разработки.

## Деплой

Скрипты в `deploy/` разворачивают приложение на выделенном хосте по SSH
(без Docker: uvicorn за Caddy, PostgreSQL и Qdrant как systemd-сервисы).

```bash
cp deploy/.env.example deploy/.env   # заполнить CONCIERGE_REMOTE/CONCIERGE_DOMAIN/…
deploy/provision.sh                  # одноразовая инициализация (PostgreSQL, Caddy, uv, systemd)
deploy/deploy.sh                     # build web -> rsync -> uv sync -> alembic -> restart
curl -k https://$CONCIERGE_DOMAIN/healthz   # -> {"db":"ok","qdrant":"ok"}
```

Бэкапы: `deploy/backup.sh` по таймеру → `/srv/concierge/backups/<дата>/`;
`deploy/backup-pull.sh` скачивает локально, `deploy/restore.sh <YYYY-MM-DD>`
восстанавливает на сервере.

Подробности эксплуатации — в [AGENTS.md](AGENTS.md), дорожная карта —
в `.claude/plans/`.
