# Deploying Powerbase, and getting a URL

## Read this first

I could not hand you a live URL. The machine I build on has outbound internet
only — no inbound — so a server started there is not reachable from your
browser, and it is wiped between sessions anyway. Anything I printed as a URL
would be a lie.

What I can do is make it one command for you, on a machine you control. That is
what this document is.

There are three ways to get a URL. Pick one.

---

## Path A — localhost, no Docker (fastest, ~2 minutes)

You need Python 3.12+.

```bash
cd powerbase-server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export POWERBASE_ADMIN_PASSWORD="$(openssl rand -hex 24)"   # required for the console
python scripts/bootstrap.py --app-id com.example.shop --name "Example Shop"
python run.py
```

**Your URL is `http://localhost:8080`.**

- Console: <http://localhost:8080/>
- API docs (Swagger UI): <http://localhost:8080/docs>
- OpenAPI spec: <http://localhost:8080/openapi.json>

`bootstrap.py` prints two keys. **Copy them now** — only the Argon2 hash is
stored, so they can never be shown again. Lose them and you rotate instead
(`POST /v1/keys/{id}/rotate`, or the CLI).

This is fine for trying it. It is not a deployment: it binds plain HTTP, runs in
your terminal, and stops when you close it.

---

## Path B0 — your own server, no Docker

If the box has no Docker and you would rather not add it, there is a bare-metal
installer that does the same job with systemd:

```bash
scp -r powerbase-server you@<server-ip>:~/
ssh you@<server-ip>
sudo bash powerbase-server/scripts/install.sh logs.example.com com.acme.shop "Acme Shop"
```

It installs python3 and (with a domain) Caddy, creates a system user, puts the
code in `/opt/powerbase`, state in `/var/lib/powerbase`, config in
`/etc/powerbase`, runs the server under a hardened systemd unit, and puts a
`powerbase` command on your PATH to manage it.

**URL:** `https://logs.example.com` — or `http://<server-ip>:8080` without a
domain.

It is idempotent: run it again from a new source tree to upgrade in place
without touching the database or the configuration.

Day-to-day operation is in **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

---

## Path B — a real domain on a VPS, with Docker


You need: a small VPS (1 vCPU / 1 GB is plenty for one team's telemetry), a
domain, and Docker.

**1. Point DNS at the box.** Add an A record: `logs.example.com -> <server IP>`.
Wait for it to resolve (`dig +short logs.example.com`). Caddy cannot get a
certificate for a name that does not resolve to it yet.

**2. Get the code onto the box** and run one command:

```bash
scp -r powerbase-server you@<server-ip>:~/
ssh you@<server-ip>
cd powerbase-server
bash setup.sh logs.example.com com.example.shop "Example Shop"
```

That script:

- refuses to run if Docker or the compose plugin is missing, and tells you what
  to install
- writes `.env` from `.env.example` and generates a random admin password
  (saved to `.admin-password`, mode 600)
- builds the image and starts the server **plus Caddy**
- waits until `/v1/health` answers before continuing
- registers your first project and prints its two keys
- prints the URLs

**Your URL is `https://logs.example.com`.**

Caddy obtains and renews the certificate itself. There is no certbot, no cron
job, and nothing to remember in 90 days.

Open ports 80 and 443. Nothing else needs to be public — the app container is
not published to the host, only Caddy is.

### What you get

| | |
|---|---|
| Console | `https://logs.example.com/` |
| API base | `https://logs.example.com/v1` |
| Swagger UI | `https://logs.example.com/docs` |
| OpenAPI spec | `https://logs.example.com/openapi.json` |
| Health | `https://logs.example.com/v1/health` |

### Day-to-day

```bash
docker compose logs -f powerbase          # follow the server log
docker compose exec powerbase ls /data/backups   # verified nightly backups
docker compose exec powerbase python scripts/maintenance.py   # run one by hand
docker compose pull && docker compose up -d --build            # upgrade
docker compose down                        # stop (add -v to delete the data too)
```

### Back up the one thing that matters

The database is a single file inside the `powerbase-data` volume. The server
writes its own verified backups to `/data/backups` nightly and keeps 7 by
default. Copy them off the box — a backup on the same disk as the database is
not a backup:

```bash
docker compose exec powerbase tar -czf - /data/backups | cat > powerbase-backups.tgz
```

The key material is not in the database. If you lose the box without a copy of
the volume, you lose the keys and the history.

---

## Path C — a managed platform

The `Dockerfile` is standard, so Fly.io, Render, Railway and friends can build
it. Two things to get right, and they are the same two things everywhere:

1. **Mount a persistent volume at `/data`.** Without it the SQLite file is on
   the container's ephemeral filesystem and every deploy wipes your telemetry.
   This is the single most common way to lose the data.
2. **Set `POWERBASE_ADMIN_PASSWORD`** as a secret. Without it the console and
   the key-administration API are disabled (`503`) and only ingestion works.

Set `POWERBASE_DB=/data/powerbase.db` and `POWERBASE_BACKUP_DIR=/data/backups`
(the image already defaults to both).

Then bootstrap a project once, from a shell on the platform:

```bash
python scripts/bootstrap.py --app-id com.example.shop --name "Example Shop"
```

TLS is terminated by the platform, so skip the Caddy service.

---

## After it is up

**Create keys through the API** rather than the CLI, so rotation has a
paper trail:

```bash
export PB=https://logs.example.com
export ADMIN=your-admin-password

curl -sS -X POST "$PB/v1/keys" \
  -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"com.example.shop","scope":"ingest","label":"website","expires_in_days":365}'
```

Two rules worth stating plainly, because they are the point of the design:

- **The ingest key is public.** It goes into a browser bundle or an APK, so
  anyone can read it out. It can write to its own project and nothing else — it
  cannot read, and it cannot mint or renew keys.
- **The read key is a secret.** It stays on the server side. Never put it in a
  client.

Rotate without downtime: `POST /v1/keys/{id}/rotate` mints the replacement
*before* revoking the old one, so a deploy can pick up the new token while the
old one is still briefly valid.

---

## Hardening checklist

- [ ] `POWERBASE_ADMIN_PASSWORD` is long and random, and not reused
- [ ] The read key is not in any client bundle or repository
- [ ] Ingest keys have an `expires_in_days`
- [ ] Firewall allows 80/443 only; the app port is not published
- [ ] Backups are copied off the box on a schedule you have actually tested
- [ ] `POWERBASE_MAINTENANCE_ENABLED=1` (the default) so pruning, rollups and
      backups run
- [ ] `POWERBASE_DISABLE_REDACTION` is `0` (the default)
- [ ] You have restored a backup into a scratch directory at least once

## What this is not

One SQLite file over one process is right for a team's own telemetry. It is not
a warehouse. If you are taking billions of events a day, this is the wrong tool
and the roadmap says so too — that is what the pluggable log-store step is for.
