# Powerbase operator runbook

For running your own Powerbase. Everything here assumes the bare-metal install
(`scripts/install.sh`); the Docker paths are the same commands with
`docker compose exec powerbase` in front.

Paths used throughout:

| | |
|---|---|
| Code | `/opt/powerbase` |
| Config | `/etc/powerbase/powerbase.env` |
| Admin password | `/etc/powerbase/admin-password` |
| Database | `/var/lib/powerbase/powerbase.db` |
| Backups | `/var/lib/powerbase/backups` |
| Service | `systemctl … powerbase` |

---

## Is it up?

```bash
powerbase status
```

That is the first thing to run, and it is safe to run when things are broken —
it reads the database directly rather than going through the server, so it works
when the server does not.

It reports the server process, the database, row counts, the last thing that
arrived, the maintenance ledger and the backups.

```
Powerbase 0.8.0
  server process             up  (v0.8.0, http://127.0.0.1:8080/v1/status/version)
  database                   /var/lib/powerbase/powerbase.db
  database reachable         yes
  ...
```

Three answers are possible for `server process`, and they mean different things:

| Says | Means | Do |
|---|---|---|
| `up` | This Powerbase answered and identified itself | Nothing |
| `not answering` | Nothing is listening on that port | `systemctl status powerbase`, then `journalctl -u powerbase -n 50` |
| `port is busy with something else` | Something answered, but it is not Powerbase | Something else owns 8080. Find it, or move Powerbase to another port |

That third case is why the check verifies identity rather than trusting a `200`.

For the full operator view over HTTP:

```bash
curl -sS -H "X-Admin-Password: $(cat /etc/powerbase/admin-password)" \
  http://127.0.0.1:8080/v1/status | python3 -m json.tool
```

---

## Common jobs

### Add a project

```bash
powerbase app create com.acme.shop "Acme Shop" web
```

Prints an ingest key and a read key. **Copy them now** — only the Argon2 hash is
stored, so they cannot be shown again. Losing one is not a problem; rotate it.

### Issue another key

```bash
powerbase key create --app com.acme.shop --scope ingest --label "web checkout" --expires-in-days 365
```

### Rotate a key

```bash
powerbase keys --app com.acme.shop      # find the key id
powerbase key rotate <key_id>
```

Rotation mints the replacement **before** revoking the old one, so there is no
window in which neither key works. Deploy the new token, then the old one is
already dead.

### Revoke a key

```bash
powerbase key revoke <key_id>
```

Immediate. The key row stays with `revoked_at` set, so you keep the history.

### Back up now

```bash
powerbase backup
```

Writes an online backup, verifies it with `PRAGMA integrity_check` and row
counts, then rotates old copies out (7 by default). The nightly pass does this
anyway; run it by hand before anything risky.

### Restore a backup

```bash
powerbase backups
systemctl stop powerbase
powerbase restore /var/lib/powerbase/backups/powerbase-20261022T010000Z.db --force
systemctl start powerbase
powerbase status
```

`--force` moves the existing database aside to `*.before-restore` rather than
deleting it, so a restore you regret is recoverable.

**Run this drill at least once, on a copy, before you need it.** A backup you
have never restored is a hope, not a backup.

### Run the nightly pass by hand

```bash
powerbase maintenance            # what has the scheduler actually done?
powerbase maintenance --daily    # roll up yesterday, prune, compact, back up
```

The pass is idempotent — the rollup and the prune both refuse to do work twice
for the same day.

### Change a setting

```bash
sudoedit /etc/powerbase/powerbase.env
sudo systemctl restart powerbase
powerbase env                    # confirm what the process will actually read
```

`powerbase env` masks the admin password and shows every resolved value, which
is the fastest way to catch a setting that did not take.

---

## When something is wrong

### Nothing arrives, but the server is up

```bash
powerbase status                 # look at "last_batch_at" / "last_log_event_at"
curl -sS http://127.0.0.1:8080/v1/alerts \
  -H "Authorization: Bearer <read key>" | python3 -m json.tool
```

The `no_telemetry` alert rule exists for exactly this. If it is firing, the
client stopped sending — check the client's key, its endpoint and whether it is
being rate limited (`429`).

### A client gets 401

The key is wrong, revoked, or expired.

```bash
powerbase keys --app com.acme.shop
```

`expired` and `revoked` are shown as such. If it looks fine, the client is
probably sending the wrong project's key — a key is bound to one project, and an
`app_id` that is not its own is a `403`, not a `401`.

### A client gets 403

The key is valid but not allowed here. Two causes: wrong scope (an ingest key
cannot read, a read key cannot write), or an `app_id` that belongs to another
project.

### A client gets 429

Rate limited. Two buckets, and both must allow: per key and per project. The
response carries `Retry-After` in seconds. If it is the project bucket, the
whole project is over budget, not one key.

### The console will not let you in

```bash
cat /etc/powerbase/admin-password
```

If `POWERBASE_ADMIN_PASSWORD` is unset, the console and the key-administration
API are disabled (`503`) by design and only ingestion works. That is a
deliberate fail-closed, not a bug.

### The disk is filling

```bash
powerbase status                 # database size
du -sh /var/lib/powerbase/backups
```

Backups are usually the cause. Lower `POWERBASE_BACKUP_KEEP`. Raw events are
pruned by retention automatically; `log_events` retention arrives in a later
step, so that table grows unbounded until then.

### The database is locked

The writer is a single process by design. If `journalctl` shows
`database is locked`, something else has the file open — most often a second
copy of the server, or a restore done while the service was running.

---

## Housekeeping

```bash
journalctl -u powerbase -f                       # follow the log
journalctl -u powerbase --since "1 hour ago"     # recent
journalctl -u caddy -n 30                        # TLS problems live here
```

Log lines are structured (`key=value`, or JSON with `POWERBASE_LOG_JSON=1`) and
the access log carries the project and key id for every request.

### Upgrading

```bash
# on your machine
scp -r powerbase-server you@host:~/
# on the host
sudo bash ~/powerbase-server/scripts/install.sh logs.example.com
```

The installer is idempotent. It replaces the code, leaves the venv, the
configuration, the database and the backups alone, and restarts the service.

### Firewall

Only 80 and 443 need to be public. With Caddy in front, the app binds
`127.0.0.1` and is not reachable from outside even if 8080 is open.

---

## Known gaps

Stated plainly, because an operator should not have to discover them:

- **`log_events` has no retention yet.** The prune covers the analytics tables;
  the observability table grows until retention is extended to it. Watch the
  database size on a busy install.
- **The nightly scheduler is in-process.** If the service is down at the
  scheduled hour, the pass is skipped for that day rather than queued. Set
  `POWERBASE_MAINTENANCE_ENABLED=0` and drive `scripts/maintenance.py --daily`
  from cron if you would rather it ran independently of the web process.
- **Single writer.** One process, one SQLite file. Do not run two instances
  against the same database.
- **No per-user accounts yet.** One admin password. Project roles and an audit
  log are a later step.
