#!/usr/bin/env bash
#
# Powerbase — install on your own server, without Docker.
#
#   sudo bash scripts/install.sh                          # plain HTTP on :8080
#   sudo bash scripts/install.sh logs.example.com         # + Caddy and a real certificate
#   sudo bash scripts/install.sh logs.example.com com.acme.shop "Acme Shop" web
#
#   bash scripts/install.sh --dry-run                     # render, install nothing
#
# `--dry-run` writes every file this script would write into a staging directory
# instead of onto the system, and runs none of the commands that change it. It
# exists so CI can check the artefacts this script produces — the systemd unit,
# the environment file, the Caddyfile — without a root shell and a systemd host.
# It does **not** prove the real install works; only a real host does that.
#
# Tested on Debian 12 / Ubuntu 22.04+. It installs python3, creates a system
# user, puts the code in /opt/powerbase, keeps state in /var/lib/powerbase,
# config in /etc/powerbase, and runs the server under systemd.
#
# Everything it does is idempotent: run it again to upgrade the code in place
# without touching the database or the configuration.

set -euo pipefail

# ------------------------------------------------------------------ dry run
# `--dry-run` can appear anywhere in the arguments, so it is filtered out here
# rather than being a fixed position.
DRY_RUN=0
ARGS=()
for _arg in "$@"; do
  if [ "$_arg" = "--dry-run" ]; then
    DRY_RUN=1
  else
    ARGS+=("$_arg")
  fi
done
set -- ${ARGS[@]+"${ARGS[@]}"}

# Where files are written. Empty means the real filesystem; in a dry run it is a
# staging directory, so the content is identical and nothing is touched.
DEST="${POWERBASE_INSTALL_DEST:-}"
if [ "$DRY_RUN" = 1 ] && [ -z "$DEST" ]; then
  DEST="$(mktemp -d -t powerbase-dryrun-XXXXXX)"
fi

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

# Run a command that changes the system, or print it. Every system-changing call
# goes through here, which is what makes `--dry-run` a property of the script
# rather than a second script that drifts from it.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    info "+ $*"
  else
    "$@"
  fi
}

# Write to a path under $DEST. In a dry run that is a staging directory; a real
# run has DEST empty, so the path is absolute as written.
dest() { printf '%s%s' "$DEST" "$1"; }

[ "$DRY_RUN" = 1 ] || [ "$(id -u)" = "0" ] || die "run this with sudo: sudo bash scripts/install.sh"
[ -n "${BASH_VERSION:-}" ] || die "run this with bash, not sh"

# In a dry run the staging tree has to exist before anything is written into it.
if [ "$DRY_RUN" = 1 ]; then
  mkdir -p "$(dest "$STATE")" "$(dest "$STATE/backups")" "$(dest "$CONF")" \
           "$(dest "$PREFIX")" "$(dest /etc/systemd/system)" \
           "$(dest /usr/local/bin)" "$(dest /etc/caddy)"
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$SOURCE_DIR/run.py" ] || die "run.py not found next to scripts/ — run this from the source tree"
[ -f "$SOURCE_DIR/requirements.txt" ] || die "requirements.txt not found in $SOURCE_DIR"

# ---------------------------------------------------------------- packages
say "installing system packages"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run bash -c 'apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates >/dev/null'

PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
[ "$PY_OK" = "1" ] || die "python3 is $(python3 -V); Powerbase needs 3.11 or newer"

if [ -n "$DOMAIN" ]; then
  info "installing Caddy for TLS on $DOMAIN"
  run bash -c 'apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null'
  if ! command -v caddy >/dev/null 2>&1; then
    run bash -c 'curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg'
    run bash -c 'curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list'
    run apt-get update -qq
    run bash -c 'apt-get install -y -qq caddy >/dev/null'
  fi
fi

# ------------------------------------------------------------------- user
say "creating the service user"
if [ "$DRY_RUN" = 1 ]; then
  info "+ useradd --system --home $STATE --shell /usr/sbin/nologin $RUN_USER"
elif ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home "$STATE" --shell /usr/sbin/nologin "$RUN_USER"
  info "created $RUN_USER"
else
  info "$RUN_USER already exists"
fi
run install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$STATE" "$STATE/backups"
run install -d -o root -g "$RUN_USER" -m 0750 "$CONF"

# ------------------------------------------------------------------- code
say "installing the application into $PREFIX"
run install -d -o root -g root -m 0755 "$PREFIX"
# --delete keeps the tree honest on an upgrade, but the venv and the data are
# outside it, so neither is touched.
if [ "$DRY_RUN" = 1 ]; then
  info "+ copy $SOURCE_DIR/ -> $PREFIX/ (excluding .git, .venv, __pycache__, *.db, backups)"
elif command -v rsync >/dev/null 2>&1; then
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
run chown -R root:root "$PREFIX"

say "installing python dependencies (into $PREFIX/.venv)"
if [ "$DRY_RUN" = 1 ]; then
  info "+ python3 -m venv $PREFIX/.venv"
  info "+ $PREFIX/.venv/bin/pip install -r $PREFIX/requirements.txt"
else
  if [ ! -x "$PREFIX/.venv/bin/python" ]; then
    python3 -m venv "$PREFIX/.venv"
  fi
  "$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
  "$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"
  chown -R root:root "$PREFIX/.venv"
fi

# -------------------------------------------------------------------- CLI
# A wrapper rather than a symlink: the CLI must run with this instance's
# interpreter and read this instance's config, not whatever python3 is first on
# PATH.
say "installing the 'powerbase' command"
cat > "$(dest /usr/local/bin/powerbase)" <<WRAPPER
#!/bin/sh
# Installed by Powerbase. Runs the CLI with the instance's own interpreter.
POWERBASE_ENV_FILE=$CONF/powerbase.env exec $PREFIX/.venv/bin/python $PREFIX/scripts/powerbase "\$@"
WRAPPER
run chmod 0755 "$(dest /usr/local/bin/powerbase)"
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

  cat > "$(dest "$CONF/powerbase.env")" <<ENV
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
  run chown root:"$RUN_USER" "$(dest "$CONF/powerbase.env")"
  run chmod 0640 "$(dest "$CONF/powerbase.env")"
  printf '%s\n' "$ADMIN_PW" > "$(dest "$CONF/admin-password")"
  run chown root:"$RUN_USER" "$(dest "$CONF/admin-password")"
  run chmod 0640 "$(dest "$CONF/admin-password")"
  info "generated an admin password -> $CONF/admin-password"
fi

# ---------------------------------------------------------------- systemd
say "installing the systemd unit"
cat > "$(dest "/etc/systemd/system/$SERVICE.service")" <<UNIT
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

run systemctl daemon-reload
run bash -c "systemctl enable $SERVICE >/dev/null 2>&1 || true"
run systemctl restart "$SERVICE"

if [ "$DRY_RUN" = 1 ]; then
  info "+ wait for http://127.0.0.1:8080/healthz"
else
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
fi

# ------------------------------------------------------------------ first app
say "registering project '$APP_ID'"
if [ "$DRY_RUN" = 1 ]; then
  info "+ $PREFIX/scripts/bootstrap.py --app-id $APP_ID --name $APP_NAME --platform $PLATFORM"
else
  set -a
  # The directive must sit immediately above the `source` it suppresses. It was
  # originally above a one-line `set -a; . file; set +a`, where it suppressed
  # nothing — which is a finding the shellcheck job surfaced.
  # shellcheck disable=SC1091
  . "$CONF/powerbase.env"
  set +a
  BOOTSTRAP_OUT="$("$PREFIX/.venv/bin/python" "$PREFIX/scripts/bootstrap.py" \
    --app-id "$APP_ID" --name "$APP_NAME" --platform "$PLATFORM")"
  printf '%s\n' "$BOOTSTRAP_OUT"
fi

# --------------------------------------------------------------------- TLS
if [ -n "$DOMAIN" ]; then
  say "configuring Caddy for $DOMAIN"
  cat > "$(dest /etc/caddy/Caddyfile)" <<CADDY
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
  run bash -c 'systemctl enable caddy >/dev/null 2>&1 || true'
  run systemctl restart caddy
  info "Caddy will obtain the certificate once DNS for $DOMAIN points here"
  BASE="https://$DOMAIN"
else
  BASE="http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
  [ "$BASE" = "http://:8080" ] && BASE="http://localhost:8080"
fi

if [ "$DRY_RUN" = 1 ]; then
  say "dry run — nothing was installed"
  info "staged under: $DEST"
  info "rendered:"
  find "$DEST" -type f | sed "s|^$DEST|  |" | sort
  cat <<EOF

  This proves the artefacts this script writes. It does not prove the install
  works: that needs a real host, root, and systemd. The unit file above can be
  checked with:  systemd-analyze verify $DEST/etc/systemd/system/$SERVICE.service
EOF
  exit 0
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
