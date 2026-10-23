#!/usr/bin/env bash
#
# Powerbase — install on your own server, without Docker.
#
#   sudo bash scripts/install.sh                          # plain HTTP on :8080
#   sudo bash scripts/install.sh logs.example.com         # + Caddy and a real certificate
#   sudo bash scripts/install.sh logs.example.com com.acme.shop "Acme Shop" web
#
# Tested on Debian 12 / Ubuntu 22.04+. It installs python3, creates a system
# user, puts the code in /opt/powerbase, keeps state in /var/lib/powerbase,
# config in /etc/powerbase, and runs the server under systemd.
#
# Everything it does is idempotent: run it again to upgrade the code in place
# without touching the database or the configuration.

set -euo pipefail

DOMAIN="${1:-}"
APP_ID="${2:-com.example.app}"
APP_NAME="${3:-Example App}"
PLATFORM="${4:-android}"

PREFIX=/opt/powerbase
STATE=/var/lib/powerbase
CONF=/etc/powerbase
SERVICE=powerbase
RUN_USER=powerbase

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this with sudo: sudo bash scripts/install.sh"
[ -n "${BASH_VERSION:-}" ] || die "run this with bash, not sh"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SOURCE_DIR/run.py" ] || die "run.py not found next to scripts/ — run this from the source tree"
[ -f "$SOURCE_DIR/requirements.txt" ] || die "requirements.txt not found in $SOURCE_DIR"

# ---------------------------------------------------------------- packages
say "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null

PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
[ "$PY_OK" = "1" ] || die "python3 is $(python3 -V); Powerbase needs 3.11 or newer"

if [ -n "$DOMAIN" ]; then
  info "installing Caddy for TLS on $DOMAIN"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
fi

# ------------------------------------------------------------------- user
say "creating the service user"
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home "$STATE" --shell /usr/sbin/nologin "$RUN_USER"
  info "created $RUN_USER"
else
  info "$RUN_USER already exists"
fi
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$STATE" "$STATE/backups"
install -d -o root -g "$RUN_USER" -m 0750 "$CONF"

# ------------------------------------------------------------------- code
say "installing the application into $PREFIX"
install -d -o root -g root -m 0755 "$PREFIX"
# --delete keeps the tree honest on an upgrade, but the venv and the data are
# outside it, so neither is touched.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude '*.db' --exclude 'backups' --exclude '.pytest_cache' \
    "$SOURCE_DIR"/ "$PREFIX"/
else
  find "$PREFIX" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  cp -a "$SOURCE_DIR"/. "$PREFIX"/
  rm -rf "$PREFIX/.git" "$PREFIX/.venv" "$PREFIX/__pycache__" "$PREFIX/.pytest_cache"
  find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
chown -R root:root "$PREFIX"

say "installing python dependencies (into $PREFIX/.venv)"
if [ ! -x "$PREFIX/.venv/bin/python" ]; then
  python3 -m venv "$PREFIX/.venv"
fi
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"
chown -R root:root "$PREFIX/.venv"

# -------------------------------------------------------------------- CLI
# A wrapper rather than a symlink: the CLI must run with this instance's
# interpreter and read this instance's config, not whatever python3 is first on
# PATH.
say "installing the 'powerbase' command"
cat > /usr/local/bin/powerbase <<WRAPPER
#!/bin/sh
# Installed by Powerbase. Runs the CLI with the instance's own interpreter.
POWERBASE_ENV_FILE=$CONF/powerbase.env exec $PREFIX/.venv/bin/python $PREFIX/scripts/powerbase "\$@"
WRAPPER
chmod 0755 /usr/local/bin/powerbase
info "powerbase -> /usr/local/bin/powerbase"

# ----------------------------------------------------------------- config
say "writing $CONF/powerbase.env"
if [ -f "$CONF/powerbase.env" ]; then
  info "keeping the existing configuration (delete it to start over)"
else
  ADMIN_PW="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  # Bind to loopback when Caddy is in front; otherwise listen on all interfaces.
  BIND_HOST="0.0.0.0"
  [ -n "$DOMAIN" ] && BIND_HOST="127.0.0.1"

  cat > "$CONF/powerbase.env" <<ENV
# Powerbase configuration. Managed by scripts/install.sh.
POWERBASE_DB=$STATE/powerbase.db
POWERBASE_BACKUP_DIR=$STATE/backups
POWERBASE_HOST=$BIND_HOST
POWERBASE_PORT=8080
POWERBASE_ADMIN_PASSWORD=$ADMIN_PW
POWERBASE_LOG_LEVEL=INFO
POWERBASE_MAINTENANCE_ENABLED=1
POWERBASE_ALERTS_ENABLED=1
POWERBASE_REDACTION_ENABLED=1
ENV
  chown root:"$RUN_USER" "$CONF/powerbase.env"
  chmod 0640 "$CONF/powerbase.env"
  printf '%s\n' "$ADMIN_PW" > "$CONF/admin-password"
  chown root:"$RUN_USER" "$CONF/admin-password"
  chmod 0640 "$CONF/admin-password"
  info "generated an admin password -> $CONF/admin-password"
fi

# ---------------------------------------------------------------- systemd
say "installing the systemd unit"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Powerbase telemetry server
Documentation=file://$PREFIX/README-DEPLOY.md
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$PREFIX
EnvironmentFile=$CONF/powerbase.env
ExecStart=$PREFIX/.venv/bin/python $PREFIX/run.py
Restart=on-failure
RestartSec=3

# This process accepts untrusted input from the internet. Give it no more than
# it needs: no privileges, no writable code, no access to the rest of the disk.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=$STATE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"

say "waiting for the server to answer"
for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:8080/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS "http://127.0.0.1:8080/healthz" >/dev/null 2>&1 \
  || die "the service never became healthy. Check: journalctl -u $SERVICE -n 50"
info "healthy"

# ------------------------------------------------------------------ first app
say "registering project '$APP_ID'"
# shellcheck disable=SC1091
set -a; . "$CONF/powerbase.env"; set +a
BOOTSTRAP_OUT="$("$PREFIX/.venv/bin/python" "$PREFIX/scripts/bootstrap.py" \
  --app-id "$APP_ID" --name "$APP_NAME" --platform "$PLATFORM")"
printf '%s\n' "$BOOTSTRAP_OUT"

# --------------------------------------------------------------------- TLS
if [ -n "$DOMAIN" ]; then
  say "configuring Caddy for $DOMAIN"
  cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8080
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		-Server
	}
}
CADDY
  systemctl enable caddy >/dev/null 2>&1 || true
  systemctl restart caddy
  info "Caddy will obtain the certificate once DNS for $DOMAIN points here"
  BASE="https://$DOMAIN"
else
  BASE="http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
  [ "$BASE" = "http://:8080" ] && BASE="http://localhost:8080"
fi

cat <<EOF

========================================================================
  Powerbase is installed and running.

  Console : $BASE/
  API     : $BASE/v1
  Docs    : $BASE/docs
  Status  : $BASE/status

  Admin password : $(cat "$CONF/admin-password" 2>/dev/null || echo "see $CONF/powerbase.env")

  The keys printed above are shown once. Copy them now.

  Manage it   : powerbase status
  Logs        : journalctl -u $SERVICE -f
  Restart     : systemctl restart $SERVICE
  Upgrade     : re-run this script from the new source tree
  Config      : $CONF/powerbase.env
  Data        : $STATE/powerbase.db
  Backups     : $STATE/backups
========================================================================
EOF
