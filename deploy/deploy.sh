#!/usr/bin/env bash
# Deploy concierge server to box: rsync -> uv sync -> alembic upgrade -> restart.
set -euo pipefail

REMOTE="${CONCIERGE_REMOTE:-box}"
DEST="${CONCIERGE_DEST:-/srv/concierge/app}"

cd "$(dirname "$0")/.."

rsync -az --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    --exclude '*.egg-info' \
    server/ "$REMOTE:$DEST/server/"

ssh "$REMOTE" "cd $DEST/server && \
    \$HOME/.local/bin/uv sync --frozen --no-dev && \
    set -a && . /etc/concierge/.env && set +a && \
    .venv/bin/alembic upgrade head && \
    sudo -n systemctl restart concierge.service && \
    systemctl is-active concierge.service"
