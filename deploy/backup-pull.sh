#!/usr/bin/env bash
# Pull backups from the app server: rsync /srv/concierge/backups -> $CONCIERGE_BACKUP_DIR
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

REMOTE="${CONCIERGE_REMOTE:?set CONCIERGE_REMOTE in deploy/.env}"
DEST="${CONCIERGE_BACKUP_DIR:-$HOME/concierge-backups}"

mkdir -p "$DEST"
rsync -az "$REMOTE:/srv/concierge/backups/" "$DEST/"
echo "pulled -> $DEST"
