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

rsync -az deploy/ "$REMOTE:$DEST/deploy/"

scp deploy/Caddyfile deploy/concierge-backup.service deploy/concierge-backup.timer \
    "$REMOTE:~/"

ssh "$REMOTE" "cd $DEST/server && \
    \$HOME/.local/bin/uv sync --frozen --no-dev && \
    set -a && . /etc/concierge/.env && set +a && \
    .venv/bin/alembic upgrade head && \
    chmod +x $DEST/deploy/backup.sh $DEST/deploy/restore.sh && \
    sudo -n install -m 644 -b ~/Caddyfile /etc/caddy/Caddyfile && \
    sudo -n install -m 644 ~/concierge-backup.service /etc/systemd/system/ && \
    sudo -n install -m 644 ~/concierge-backup.timer /etc/systemd/system/ && \
    rm -f ~/Caddyfile ~/concierge-backup.service ~/concierge-backup.timer && \
    sudo -n systemctl daemon-reload && \
    sudo -n systemctl enable --now concierge-backup.timer && \
    sudo -n systemctl reload caddy && \
    sudo -n systemctl restart concierge.service && \
    systemctl is-active concierge.service"
