#!/usr/bin/env bash
#
# Powerbase — one-command setup.
#
#   ./setup.sh                      # runs on localhost
#   ./setup.sh logs.example.com     # a real domain; Caddy gets the certificate
#   ./setup.sh logs.example.com com.acme.shop "Acme Shop" web
#
# Builds the image, generates an admin password, starts the stack, waits until
# the health endpoint answers, and registers a first project. Installs nothing
# except by telling you what is missing.

set -euo pipefail

DOMAIN="${1:-localhost}"
APP_ID="${2:-com.example.app}"
APP_NAME="${3:-Example App}"
PLATFORM="${4:-android}"

cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. For a run without Docker, see README-DEPLOY.md, path A."
docker compose version >/dev/null 2>&1 \
  || die "The 'docker compose' plugin is missing (package: docker-compose-plugin)."

# --- configuration ----------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  # Hex, so the value needs no quoting anywhere it is pasted.
  if command -v openssl >/dev/null 2>&1; then
    PW="$(openssl rand -hex 24)"
  else
    PW="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  # `-i.bak` is the one form GNU sed and BSD sed agree on.
  sed -i.bak "s|^POWERBASE_ADMIN_PASSWORD=.*|POWERBASE_ADMIN_PASSWORD=$PW|" .env && rm -f .env.bak
  printf '%s\n' "$PW" > .admin-password
  chmod 600 .admin-password
  say "generated an admin password -> .admin-password"
else
  say "keeping the existing .env"
fi

if grep -q '^POWERBASE_DOMAIN=' .env; then
  sed -i.bak "s|^POWERBASE_DOMAIN=.*|POWERBASE_DOMAIN=$DOMAIN|" .env && rm -f .env.bak
else
  printf '\n# Domain Caddy serves. "localhost" for a local run.\nPOWERBASE_DOMAIN=%s\n' "$DOMAIN" >> .env
fi

# --- start ------------------------------------------------------------------
say "starting the stack (the first build takes a minute)…"
docker compose up -d --build

say "waiting for the health endpoint…"
healthy=""
for _ in $(seq 1 60); do
  if docker compose exec -T powerbase curl -fsS http://127.0.0.1:8080/v1/health >/dev/null 2>&1; then
    healthy="yes"
    break
  fi
  sleep 2
done
[ -n "$healthy" ] || die "the server never became healthy. Try: docker compose logs powerbase"
say "healthy."

# --- first project ----------------------------------------------------------
say ""
say "registering project '$APP_ID'…"
docker compose exec -T powerbase python scripts/bootstrap.py \
  --app-id "$APP_ID" --name "$APP_NAME" --platform "$PLATFORM"

# --- where it is ------------------------------------------------------------
if [ "$DOMAIN" = "localhost" ]; then
  BASE="https://localhost"
  ALT="http://localhost:8080  (uncomment the port mapping in docker-compose.yml)"
else
  BASE="https://$DOMAIN"
  ALT=""
fi

ADMIN="$(cat .admin-password 2>/dev/null || echo 'see POWERBASE_ADMIN_PASSWORD in .env')"

cat <<EOF

========================================================================
  Powerbase is running.

  Console : $BASE/
  API     : $BASE/v1
  Docs    : $BASE/docs            (Swagger UI, every endpoint)
  Spec    : $BASE/openapi.json    (OpenAPI 3.1)

  Admin password : $ADMIN

  The two keys printed above are shown once. Copy them now:
    - the ingest key goes into your app or SDK
    - the read key is what the console and API clients use

  Data    : the 'powerbase-data' volume (/data/powerbase.db in the container)
  Backups : docker compose exec powerbase ls /data/backups
  Logs    : docker compose logs -f powerbase
  Stop    : docker compose down        (add -v to delete the data too)
EOF

if [ -n "$ALT" ]; then
  printf '\n  Without Caddy: %s\n' "$ALT"
fi
say "========================================================================"
