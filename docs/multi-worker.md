# Running more than one worker

This build expects **one process**. That is not a limitation it hides — it is one
it now states, at startup and in `/v1/status`, because the failure mode is quiet.

## What goes wrong with more than one

Two things live inside the process and count in memory.

### Rate limits multiply

The per-project token bucket is in-process. With N workers a project gets **N
independent budgets**, so the limit you configured is effectively N times what
it says:

| configured | one worker | four workers |
|---|---|---|
| `POWERBASE_PROJECT_RATE_PER_SECOND=10` | 10/s | **40/s** |
| `POWERBASE_PROJECT_RATE_BURST=60` | 60 | **240** |

Nothing breaks. The number in your config is just wrong, and it stays wrong
until someone wonders why a client is getting four times the throughput.

### The scheduler runs N times

The nightly rollup, prune and backup run on a thread inside the process. With N
workers, N schedulers run.

The **job ledger** interlocks the daily pass, so the rollup and prune do not
happen twice a day. But the interval passes — alerts, incidents, drains — each
run once per worker, and the ledger is the only thing standing between a
multi-worker deployment and duplicated work.

## What the build does about it

At startup, if more than one worker is configured:

```
deployment: 4 workers are configured. This build expects one process.
deployment: Rate limits are counted per process, so each project gets 4x the
            configured budget (100.0/s and 600 burst becomes 400.0/s and 2400
            burst in total).
deployment: The in-process scheduler is on, so 4 schedulers will run. ...
```

The count comes from `POWERBASE_WORKERS`, falling back to `WEB_CONCURRENCY`
(which uvicorn and gunicorn set), falling back to 1. **`POWERBASE_WORKERS`
wins**, so `POWERBASE_WORKERS=1` is how you say "I know, it is one process".

`/v1/status` carries the same thing, so it is visible without reading logs:

```json
"deployment": {
  "workers": 4,
  "supported_workers": 1,
  "single_process": false,
  "rate_limit_scope": "process",
  "effective_project_rate_per_second": 400.0,
  "effective_project_rate_burst": 2400,
  "scheduler_in_process": true,
  "warnings": ["…"]
}
```

A single-process deployment gets `single_process: true` and an empty `warnings`
list. It must stay silent — a warning that fires on the supported shape is
wallpaper.

## What to do instead

**Run one worker, and scale by running more instances behind a proxy.** That
gives you the concurrency without the arithmetic problem.

If you need several workers in one process group, turn the scheduler off and
drive it from cron:

```bash
POWERBASE_MAINTENANCE_ENABLED=0
```

```cron
# the nightly pass, once, on one host
17 2 * * * /opt/powerbase/.venv/bin/python /opt/powerbase/scripts/maintenance.py --daily
```

and divide your configured rate limits by the worker count so the effective
limit is what you meant.

## Not done

- **No shared rate-limit store.** Redis or a SQLite-backed bucket would make the
  limit exact across processes. It costs a round trip per request, which is why
  it has not been done for a single-node build — `powerbase/ratelimit.py` says
  the interface would not change if it were.
- **No scheduler leader election.** A lease in the job ledger would let N
  workers elect one scheduler. The ledger already exists; the election does not.
