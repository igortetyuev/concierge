#!/usr/bin/env bash
# One-time provisioning of box (ALT Linux): PostgreSQL, Caddy, uv, dirs, env file.
# Idempotent; safe to re-run. Existing /etc/concierge/.env (secrets) is preserved.
set -euo pipefail

REMOTE="${CONCIERGE_REMOTE:-box}"

cd "$(dirname "$0")"

# Copy unit + Caddyfile to a staging dir, then install with sudo.
ssh "$REMOTE" 'mkdir -p ~/concierge-provision'
scp concierge.service Caddyfile "$REMOTE:~/concierge-provision/"

ssh "$REMOTE" 'bash -s' <<'REMOTE'
set -euo pipefail

sudo -n apt-get update -qq
sudo -n apt-get install -y postgresql14-server caddy ffmpeg curl

# PostgreSQL: initdb if needed (peer auth local, scram over TCP), localhost only.
if ! sudo -n test -s /var/lib/pgsql/data/postgresql.conf; then
    sudo -n -u postgres initdb -D /var/lib/pgsql/data \
        --encoding=UTF8 --locale=C.utf8 \
        --auth-local=peer --auth-host=scram-sha-256
fi
sudo -n sed -i "s/^#\?listen_addresses.*/listen_addresses = 'localhost'/" \
    /var/lib/pgsql/data/postgresql.conf
sudo -n systemctl enable --now postgresql

# DB user + database (idempotent; password set below only on first run).
sudo -n -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'CREATE USER concierge'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'concierge')\gexec
SELECT 'CREATE DATABASE concierge OWNER concierge'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'concierge')\gexec
SQL

# uv for the appuser user.
if ! test -x "$HOME/.local/bin/uv"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# App dirs + env file. Owned by appuser (service user), mode 600 — readable by the
# deploy step (alembic) and by systemd before dropping privileges.
sudo -n mkdir -p /srv/concierge/app /srv/concierge/blobs /etc/concierge
sudo -n chown -R appuser:appuser /srv/concierge
if ! sudo -n test -f /etc/concierge/.env; then
    DBPASS="$(openssl rand -hex 16)"
    printf 'CONCIERGE_DATABASE_URL=postgresql+asyncpg://concierge:%s@127.0.0.1:5432/concierge\nCONCIERGE_QDRANT_URL=http://127.0.0.1:6333\n' "$DBPASS" \
        | sudo -n tee /etc/concierge/.env >/dev/null
    sudo -n -u postgres psql -c "ALTER USER concierge WITH PASSWORD '${DBPASS}'"
fi
sudo -n chown appuser:appuser /etc/concierge/.env
sudo -n chmod 600 /etc/concierge/.env

# systemd unit + Caddy.
sudo -n install -m 644 ~/concierge-provision/concierge.service \
    /etc/systemd/system/concierge.service
sudo -n install -m 644 -b ~/concierge-provision/Caddyfile /etc/caddy/Caddyfile
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now caddy
rm -rf ~/concierge-provision
REMOTE

echo "Provisioned. Now run deploy/deploy.sh"
