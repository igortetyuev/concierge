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

## Telegram-бот

Включается из PWA (`/settings`, админ): `telegram.enabled` + `telegram.bot_token` (секрет —
нужен `CONCIERGE_SECRET_KEY`); супервизор подхватывает изменения без рестарта (~10 с).
Привязка аккаунта: код в `/settings` → `/start <код>` боту. Чтобы бот видел все сообщения
групп/топиков (они сохраняются в групповую сессию, ответ — только по @упоминанию/reply),
отключите privacy mode у BotFather (`/setprivacy`) или сделайте бота админом группы.
