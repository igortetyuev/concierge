#!/usr/bin/env bash
# Daily backup on the app server: pg_dump + blobs rsync + per-collection Qdrant snapshots
# -> /srv/concierge/backups/<YYYY-MM-DD>/ ; keeps the last 14 days.
set -euo pipefail

STAMP="$(date +%F)"
BASE=/srv/concierge/backups
DEST="$BASE/$STAMP"
BLOB_DIR="${CONCIERGE_BLOB_DIR:-/srv/concierge/blobs}"

set -a
# shellcheck disable=SC1091
. /etc/concierge/.env
set +a

# postgresql+asyncpg://user:pass@host:port/db -> PGPASSWORD + pg args
DBSPEC="${CONCIERGE_DATABASE_URL#*://}"
DBUSER="${DBSPEC%%:*}"
DBSPEC="${DBSPEC#*:}"
export PGPASSWORD="${DBSPEC%%@*}"
DBSPEC="${DBSPEC#*@}"
DBHOSTPORT="${DBSPEC%%/*}"
DBNAME="${DBSPEC#*/}"
DBNAME="${DBNAME%%\?*}"
DBHOST="${DBHOSTPORT%%:*}"
DBPORT="${DBHOSTPORT##*:}"
[ "$DBPORT" = "$DBHOST" ] && DBPORT=5432

QDRANT="${CONCIERGE_QDRANT_URL:-http://127.0.0.1:6333}"

mkdir -p "$DEST"

pg_dump -h "$DBHOST" -p "$DBPORT" -U "$DBUSER" -Fc "$DBNAME" > "$DEST/postgres.dump"

mkdir -p "$DEST/blobs"
rsync -a --delete "$BLOB_DIR/" "$DEST/blobs/"

for col in $(curl -sf "$QDRANT/collections" | jq -r '.result.collections[].name'); do
    curl -sf -X POST "$QDRANT/collections/$col/snapshots" >/dev/null
    snap=$(curl -sf "$QDRANT/collections/$col/snapshots" \
        | jq -r '.result | sort_by(.creation_time) | last | .name')
    curl -sf -o "$DEST/qdrant-$col.snapshot" "$QDRANT/collections/$col/snapshots/$snap"
done

find "$BASE" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +

echo "backup $STAMP done: $DEST"
