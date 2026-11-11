#!/usr/bin/env bash
# Pull backups from box to this Mac: rsync /srv/concierge/backups -> ~/concierge-backups
set -euo pipefail

REMOTE="${CONCIERGE_REMOTE:-box}"
DEST="${CONCIERGE_BACKUP_DIR:-$HOME/concierge-backups}"

mkdir -p "$DEST"
rsync -az "$REMOTE:/srv/concierge/backups/" "$DEST/"
echo "pulled -> $DEST"
