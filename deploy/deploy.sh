#!/usr/bin/env bash
# Deploy concierge to the app server: build web -> rsync server+web -> uv sync ->
# alembic upgrade -> install Caddyfile -> restart services.
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
cd ..

REMOTE="${CONCIERGE_REMOTE:?set CONCIERGE_REMOTE in deploy/.env}"
DOMAIN="${CONCIERGE_DOMAIN:?set CONCIERGE_DOMAIN in deploy/.env}"
DEST="${CONCIERGE_DEST:-/srv/concierge/app}"

(cd web && pnpm install --frozen-lockfile && pnpm build)

rsync -az --delete \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    --exclude '*.egg-info' \
    server/ "$REMOTE:$DEST/server/"

# Top-level dirs in the web root containing a .keep file belong to other
# projects — exclude them so --delete doesn't remove them.
KEEP_EXCLUDES=()
while IFS= read -r d; do
    KEEP_EXCLUDES+=(--exclude="/$d/")
done < <(ssh "$REMOTE" "for d in $DEST/web/*/; do [ -f \"\$d.keep\" ] && basename \"\$d\"; done")

rsync -az --delete ${KEEP_EXCLUDES[@]+"${KEEP_EXCLUDES[@]}"} web/dist/ "$REMOTE:$DEST/web/"

rsync -az deploy/ "$REMOTE:$DEST/deploy/"

CADDY_TMP="$(mktemp)"
sed "s/__DOMAIN__/${DOMAIN}/g" deploy/Caddyfile > "$CADDY_TMP"
scp "$CADDY_TMP" "$REMOTE:~/Caddyfile"
rm -f "$CADDY_TMP"

scp deploy/concierge-backup.service deploy/concierge-backup.timer "$REMOTE:~/"

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
    (sudo -n install -m 644 /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt $DEST/web/root.crt || echo 'warning: caddy root CA not found') && \
    sudo -n systemctl restart concierge.service && \
    systemctl is-active concierge.service"
