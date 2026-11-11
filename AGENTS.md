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
Автозапуск — Run-ключ `Ollama`, `OLLAMA_HOST=0.0.0.0` в user env, firewall-правило «Ollama 11434».

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
