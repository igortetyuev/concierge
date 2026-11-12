#!/usr/bin/env bash
# Restore a backup on box: restore.sh <YYYY-MM-DD>
# Recreates the DB from postgres.dump, replaces blobs, uploads Qdrant snapshots.
# Stops the service during restore. Run on the app server as the service user:
# ssh <host> 'bash /srv/concierge/app/deploy/restore.sh <YYYY-MM-DD>'
set -euo pipefail

STAMP="${1:?usage: restore.sh <YYYY-MM-DD>}"
SRC="/srv/concierge/backups/$STAMP"
BLOB_DIR="${CONCIERGE_BLOB_DIR:-/srv/concierge/blobs}"

test -f "$SRC/postgres.dump"

set -a
# shellcheck disable=SC1091
. /etc/concierge/.env
set +a

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

sudo -n systemctl stop concierge

sudo -n -u postgres psql -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE $DBNAME WITH (FORCE);" \
    -c "CREATE DATABASE $DBNAME OWNER $DBUSER;"
pg_restore -h "$DBHOST" -p "$DBPORT" -U "$DBUSER" -d "$DBNAME" \
    --no-owner --no-privileges "$SRC/postgres.dump"

mkdir -p "$BLOB_DIR"
rsync -a --delete "$SRC/blobs/" "$BLOB_DIR/"

for f in "$SRC"/qdrant-*.snapshot; do
    [ -e "$f" ] || continue
    col=$(basename "$f" .snapshot)
    col="${col#qdrant-}"
    curl -sf -X POST \
        "$QDRANT/collections/$col/snapshots/upload?priority=snapshot" \
        -H 'Content-Type:multipart/form-data' -F "snapshot=@$f"
done

sudo -n systemctl start concierge
systemctl is-active concierge.service
echo "restore $STAMP done"
