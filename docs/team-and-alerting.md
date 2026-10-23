# Alerts and team access (Step 5)

Knowing something broke, and knowing who is allowed to do something about it.

---

## Alerting

### The problem this fixes

Until now a rule was evaluated on every maintenance pass and the webhook fired
every time. A problem lasting an hour produced a dozen identical notifications,
and an operator learns to ignore those. A webhook that failed was simply lost.

An **incident** is one rule failing in one project. A **delivery** is one
attempt to tell somebody, and it can be retried and eventually given up on.

### Lifecycle

```
rule fires ──► incident opened ──► delivery queued ──► sent
                    │                                    │
                    │  still firing, past the cooldown    │ fails
                    ├────────► reminder ─────────────────┤
                    │                                    ▼
                    │                            retry (backoff)
   acknowledged ────┤                                    │
   (quiet until it resolves)                    attempts exhausted
                    │                                    ▼
   rule stops ──────┴────────► resolved ────────► dead (visible)
```

Four behaviours, each with a test:

- **One open incident per (project, rule).** Enforced by a partial unique index,
  so two workers racing cannot both open one.
- **A cooldown.** A continuing problem reminds you on the schedule you chose,
  not the scheduler's. Default 60 minutes.
- **Acknowledgement is respected.** An acked incident stays quiet until it
  resolves — that is the point of acking.
- **A recovery notice.** Resolving notifies too, so "it came back" is not
  something you have to go and check.

An **escalation notifies immediately**, inside the cooldown: a warning becoming
critical is worth interrupting for.

Switching alerts off (`POWERBASE_ALERTS_ENABLED=0`) does **not** resolve
anything. The feature being off is not evidence that the problem went away.

### Channels

```bash
# instance-wide, everything from warning up
curl -sS -X POST $PB/v1/alert-channels -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"webhook","target":"https://hooks.example.com/pb","min_severity":"warning"}'

# one project only, critical only, by email
curl -sS -X POST $PB/v1/alert-channels -H "X-Admin-Password: $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"email","target":"oncall@example.com","app_id":"com.acme.shop","min_severity":"critical"}'
```

`min_severity` is how you keep a chat channel from carrying every INFO-level
notice while email only takes the critical ones.

`POWERBASE_ALERT_WEBHOOK_URL` still works: on the first pass it is adopted as an
instance-wide channel, so an existing install needs no reconfiguration.

### Delivery, retries and giving up

| Attempt | Wait before the next |
|---|---|
| 1 | 60 s |
| 2 | 120 s |
| 3 | 240 s |
| 4 | 480 s |
| 5 | — given up, marked `dead` |

A **dead** delivery is visible, not lost:

```bash
powerbase incidents --flush      # send whatever is due now
curl -sS $PB/v1/alert-queue -H "X-Admin-Password: $ADMIN"
```

```json
{"counts": {"sent": 12, "failed": 1, "dead": 1},
 "dead": [{"id": 7, "target": "https://hooks.example.com/pb",
           "attempts": 5, "last_error": "connection refused"}]}
```

Requeue one with `POST /v1/alert-deliveries/{id}/retry`.

### Email

Standard-library `smtplib` — no third-party client to keep patched.

| Variable | |
|---|---|
| `POWERBASE_SMTP_HOST` | Unset means the email channel fails with a clear reason rather than silently |
| `POWERBASE_SMTP_PORT` | 587 |
| `POWERBASE_SMTP_USER` / `POWERBASE_SMTP_PASSWORD` | |
| `POWERBASE_SMTP_FROM` | |
| `POWERBASE_SMTP_NO_STARTTLS` | `1` to disable STARTTLS |

### Acknowledging

```bash
powerbase incidents                                   # what is on fire
powerbase incidents --ack <incident-id> --actor you@example.com
powerbase incidents --resolve <incident-id> --actor you@example.com
```

```bash
curl -sS -X POST $PB/v1/incidents/$ID/ack     -H "X-Powerbase-Session: $TOKEN"
curl -sS -X POST $PB/v1/incidents/$ID/resolve -H "X-Powerbase-Session: $TOKEN"
curl -sS -X POST $PB/v1/incidents/$ID/reopen  -H "X-Powerbase-Session: $TOKEN"
```

---

## Team access

### Bootstrap the first owner

```bash
powerbase owner --email you@example.com
```

Prints a generated password **once**. Without an owner, nobody can sign in.

### The roles

Permissions are a table, not a hierarchy of if-statements, so "what can a
developer do" has one answer in one place.

| Permission | owner | admin | developer | readonly |
|---|:-:|:-:|:-:|:-:|
| `read` | ✓ | ✓ | ✓ | ✓ |
| `export` | ✓ | ✓ | ✓ | |
| `alerts:ack` | ✓ | ✓ | ✓ | |
| `alerts:write` | ✓ | ✓ | | |
| `keys:write` | ✓ | ✓ | | |
| `users:read` | ✓ | ✓ | | |
| `users:write` | ✓ | ✓ | | |
| `users:write_admin` | ✓ | | | |
| `settings:write` | ✓ | ✓ | | |
| `project:write` | ✓ | ✓ | | |

Two rules that are easy to get wrong, and are enforced:

- **Only an owner may mint an admin.** Otherwise an admin can promote
  themselves past their own ceiling.
- **The last active owner cannot be demoted or disabled.** There is always a way
  back in.

An unknown permission is **refused**, not allowed. Being wrong about a
permission must fail closed.

### Signing in

```bash
curl -sS -X POST $PB/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'
# -> {"token":"…","expires_at":"…","user":{…}}
```

Send the token as `X-Powerbase-Session: <token>`, or let the `pb_user` cookie
carry it in a browser.

```bash
curl -sS $PB/v1/auth/me -H "X-Powerbase-Session: $TOKEN"
```

```json
{"actor":"you@example.com","role":"owner",
 "permissions":["alerts:ack","alerts:write","export","keys:write","project:write",
                "read","settings:write","users:read","users:write","users:write_admin"],
 "projects":null}
```

`projects: null` means **every** project — that is how owner and admin read.
Everyone else gets a list, and a developer with no membership sees an empty one.
That is the correct default, not a bug.

### Break-glass

The admin password still works, as `X-Admin-Password`, and maps to the owner
role. It is what you use when the owner account is locked out — the recovery path
lives on the host, not in the application.

### Lockout

Five failed attempts lock the account for fifteen minutes. A lockout answers
**429**, not 401 and not 400: the credential was never examined.

A wrong password and an unknown account return the *same* message. Saying "no
such user" tells an attacker which half of the guess was right.

### Invites

```bash
curl -sS -X POST $PB/v1/invites -H "X-Powerbase-Session: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email":"new@example.com","role":"readonly","app_id":"com.acme.shop"}'
```

The response carries a `token`, shown once. The invitee accepts it:

```bash
curl -sS -X POST $PB/v1/invites/accept -H 'Content-Type: application/json' \
  -d '{"token":"…","password":"a-long-enough-pw","display_name":"New Person"}'
```

- **Single use.** A second attempt is `409`.
- **Expires** (7 days by default). Past that, `410`.
- **Scoped, optionally.** An invite with an `app_id` also adds the person to
  that project.
- An invite for someone who already has an account is a password reset plus a
  role change, not a second account.

### Project membership

```bash
curl -sS -X POST $PB/v1/projects/com.acme.shop/members -H "X-Powerbase-Session: $TOKEN" \
  -H 'Content-Type: application/json' -d '{"user_id":"…","role":"developer"}'
```

A developer scoped to one project gets `200` on that project's members and `403`
on any other. Disabling an account revokes its sessions immediately — otherwise
"disabled" would mean "disabled at the next login".

---

## The audit trail

Every one of these writes a row: `user.created`, `user.invited`, `user.login`,
`user.login_failed`, `user.logout`, `user.role_changed`, `user.disabled`,
`member.added`, `member.removed`, `incident.opened`, `incident.acknowledged`,
`incident.resolved`, `incident.reopened`, plus `denied.<permission>` for every
refusal.

A refusal is worth recording. "Who tried and was told no" is the other half of an
audit trail.

```bash
powerbase audit --type user.login_failed
powerbase audit --limit 20
```

---

## Not done yet

- **The console has no team UI.** The API and the CLI are the ways in; the
  dashboard still authenticates with the admin password alone.
- **No SSO.** The roadmap puts it last, after a protocol is implemented and
  tested.
- **`audit_events` is never pruned.** It only grows.
- **No password reset by email.** An owner sets a password, or issues an invite.
- **No per-channel secrets.** A webhook URL with a token in it is stored as-is;
  it is not encrypted at rest.
