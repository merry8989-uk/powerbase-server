# Privacy notice

> **This is a template with the facts of the implementation filled in.** Anything
> in square brackets is deployment-specific and must be completed by whoever
> runs the instance. A privacy notice that describes the software but not the
> operator is only half a notice.

Powerbase is self-hosted observability software. It runs on infrastructure its
operator provides. **The operator is the data controller** for anything stored
in their instance; this document describes what the software does with data, not
what a particular operator does with it.

## What Powerbase collects

Only what an application deliberately sends. Nothing is collected covertly, and
the server makes no outbound connection unless the operator configured one.

### Log and trace events

Applications, websites and mobile apps send events through an SDK or an
OpenTelemetry exporter. An event can carry:

- a timestamp and a severity (`TRACE` to `FATAL`)
- an event type, a category, and a service name
- an environment, an application version, and a platform version
- a request id, a trace id, and a span id
- a route **template** (`/orders/:id`), never a full URL with its query string
- an HTTP method, status code and duration
- an error code and a sanitized error fingerprint
- a message, with redaction applied
- up to 64 attribute key/value pairs, 4 levels deep, 2048 characters each

### Spans and metrics

Traces carry a span name, kind, status, duration, trace and parent ids, and
attributes. Metrics carry a name, kind, unit, time, value, and attributes.
`db.statement` is reduced to its **shape** — `SELECT * FROM users WHERE email = ?`
— never the statement with its values.

### Crash reports (Android)

If an application integrates the Android SDK, crash reports include the stack
trace, the exception class, the device model, the OS version, and the app
version. Integration is a deliberate act by the application's developer.

### Security audit events

The instance records who did what: logins and failed logins, API key creation,
rotation and revocation, role changes, permission refusals, exports, and
incident acknowledgement. An audit event carries an actor, a target, a result
and a reason code.

## What Powerbase never collects

This list is enforced by a redaction library that runs **before data leaves the
process**, and again on the way out during an export:

- passwords, password-reset secrets, one-time codes
- API keys, JWTs, refresh tokens, session cookies, authorization headers
- database URLs and credentials
- private keys, signing secrets, encryption keys
- raw request or response bodies
- uploaded file contents and signed download URLs
- payment card or bank details
- full SQL parameter values
- secrets that appear inside exception messages

An attribute whose **name** looks like a credential (`password`, `token`,
`api_key`, `secret`, `authorization`, `cookie`, `session`, `private_key`) is
dropped whole rather than redacted in place. Credential-named keys are reported
in a `dropped_attributes` count so the integrator knows to stop sending them.

The browser SDK has no code path that reads DOM contents, form values,
keystrokes, cookies, request bodies or full URLs. A test traps the DOM and fails
if one is added.

## What is not collected by default

- No product analytics, no usage tracking, no telemetry to the authors.
- No advertising identifiers, hardware serial numbers, or cross-app tracking.
- No precise location, contacts, call logs, SMS, photos, files, clipboard,
  microphone or camera data.
- No browsing history.

The server contacts nothing outside the operator's infrastructure unless the
operator configured an alert webhook, an email server, or a log drain.

## Retention

Each project has its own retention period, applied nightly. When a day is older
than the retention period **and** has been rolled up into daily aggregates, the
raw rows are deleted. The aggregate counts remain, so a chart of last year still
works while the rows themselves are gone.

Retention covers `events`, `logs`, `crashes`, `sessions`, `log_events`, `spans`
and `metric_points`. Audit events are kept indefinitely by design.

Backups are taken nightly, verified by opening them, and pruned to the
operator's configured count. [Operator: state your backup location and how long
they are kept.]

## Access

- Ingestion keys can write. They cannot read anything.
- Dashboard access requires a session, and roles decide what it can see:
  `readonly` reads, `developer` can export and acknowledge, `admin` can manage
  keys and channels, `owner` can manage people.
- A user scoped to a project sees only that project.
- Audit events are readable only by an owner or admin.
- The admin password is break-glass and maps to the owner role.

## Your rights

[Operator: state the lawful basis for processing, the identity and contact
details of the controller, and the procedure and timeframe for access,
correction, deletion and objection requests.]

Because the operator holds the data and not the authors of this software, a
request must go to the operator of the instance where the data lives.

## Changes

[Operator: state how changes to this notice will be communicated.]
