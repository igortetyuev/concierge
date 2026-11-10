#!/usr/bin/env bash
# Deploy concierge to box: build web -> rsync server+web -> uv sync ->
# alembic upgrade -> install Caddyfile -> restart services.
set -euo pipefail

REMOTE="${CONCIERGE_REMOTE:-box}"
DEST="${CONCIERGE_DEST:-/srv/concierge/app}"

cd "$(dirname "$0")/.."

(cd web && pnpm install --frozen-lockfile && pnpm build)

rsync -az --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    --exclude '*.egg-info' \
    server/ "$REMOTE:$DEST/server/"

rsync -az --delete web/dist/ "$REMOTE:$DEST/web/"

scp deploy/Caddyfile "$REMOTE:~/concierge-Caddyfile"

ssh "$REMOTE" "cd $DEST/server && \
    \$HOME/.local/bin/uv sync --frozen --no-dev && \
    set -a && . /etc/concierge/.env && set +a && \
    .venv/bin/alembic upgrade head && \
    sudo -n install -m 644 -b ~/concierge-Caddyfile /etc/caddy/Caddyfile && \
    rm -f ~/concierge-Caddyfile && \
    sudo -n systemctl reload caddy && \
    sudo -n systemctl restart concierge.service && \
    systemctl is-active concierge.service"
