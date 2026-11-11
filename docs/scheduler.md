# Scheduler

`controller/runletd.py` reconciles a ledger against reality on every pass. It holds no state in memory, so a restart resumes from the queue rather than from nothing.

## State

Everything lives in one SQLite file, set with `--state`.

```
jobs     job_id, repo, name, labels, state, attempts, first_seen, updated_at
leases   lease_id, job_id, runner_name, fencing_token, state, granted_at, ended_at
```

A job moves between `queued`, `running`, `done` and `cancelled`. A lease is the record that a container was started for a job, and it carries a fencing token that increases every time the job is retried.

## A pass

```
jobs      := queued jobs from GitHub, upserted into the ledger
for each active lease whose container is gone:
    job still queued  -> expire the lease, retry with a higher token
    job left the queue -> mark the job done
cancel queued jobs that are no longer queued upstream
dispatch: for each queued job, grant a lease and start a container
top up the warm pool to --min-warm
```

Nothing is driven by an event that could be missed. A webhook wakes the loop early, but a missed webhook costs one interval, not a job.

## What decides a retry

When a container disappears, the upstream queue decides what happened. If the job is still queued, the runner never took it and the job is retried. If the job has left the queue, the runner took it and the job is done.

Getting this wrong in either direction is expensive: retrying completed work starts duplicate containers, and treating a failed start as done loses the job.

`--grace` sets how long a missing container is tolerated before it counts as a failure, so a container that is still being created is not misread.

## Fencing

The token increases on every attempt. A runner name includes it, so the ledger and the runtime can be compared directly:

```
runlet-<job_id>-1    first attempt
runlet-<job_id>-2    retry after the first container exited
```

A stale container cannot be confused with the current one, because the name and the token differ. Retries stop at `--max-attempts`, after which the job is cancelled rather than looping.

## Warm runners

`--min-warm N` keeps N prewarm leases, each a container that is registered and listening but not tied to a job. A prewarm lease has no job id.

When a job appears and a warm runner is available, the scheduler points that lease at the job instead of starting another container. The containers are interchangeable, so the bookkeeping can move rather than duplicate.

## Capacity

`--max` caps active leases. A queued job that finds no capacity stays queued and is picked up on a later pass, so a burst of jobs is served in order rather than dropped.

## Observing a pass

```
$ python3 controller/runletd.py ... --once
observed=2 granted=2 retried=0 completed=0 cancelled=0
```

The same file can be read directly while the scheduler runs:

```console
$ sqlite3 runlet-ledger.db 'select job_id, state, attempts from jobs'
```

## Tests

`controller/test_scheduler.py` covers capacity limits, retry with a higher token, completion versus retry after a container exits, the attempt cap, the grace window, restart safety, and warm-pool behavior. The source and runtime are faked, so the tests run in milliseconds.

## Seeing what it is doing

### Logs

Every dispatch, retry, completion and cancellation prints one line naming the job, the repository and the runner:

```
22:54:49  dispatching OWNER/REPO "smoke" to runlet-130310446675-1 (labels ['runlet-runner'], attempt 1)
22:55:36  finished OWNER/REPO "smoke" after 49s (runlet-130310446675-1)
```

Failures name the reason rather than an error code:

```
retrying OWNER/REPO "build" (attempt 2 of 3): the runner exited without taking the job
giving up on OWNER/REPO "build" after 3 attempts: the runner exited without taking the job
cancelled OWNER/REPO "deploy": it is no longer queued upstream
```

### Status

```console
$ python3 controller/runletd.py status --state runlet-ledger.db --max 8
RUNNING 2 of 8
       6s  integration tests                          OWNER/REPO   attempt 2
    3m12s  build                                    OWNER/REPO

QUEUED 1
      45s  deploy                                   OWNER/REPO   runlet-runner

RECENT
       0s ago  smoke                                OWNER/REPO   ok, took 48s
     9m ago  lint                                   OWNER/REPO   ok, took 22s
```

It reads the SQLite file directly, so it works whether or not the scheduler is running. A container that has disappeared but whose lease is still active shows `[container gone, will retry]`, which is the state that used to be invisible.

Two flags make it scriptable:

```console
$ python3 controller/runletd.py status --state runlet-ledger.db --short
2 running, 1 queued
$ python3 controller/runletd.py status --state runlet-ledger.db --json
{"running": [...], "queued": [...], "recent": [...], "counts": {...}}
```

Job names, repositories, wait times, elapsed times, retry counts and recent outcomes all come from the ledger, which is why they survive a scheduler restart.

## Metrics

`--metrics-port` serves Prometheus text on `/metrics` and a readiness probe on `/healthz`. Every value is derived from the ledger, so the numbers survive a restart. Only the reconcile counters live in memory and reset with the process, which is how Prometheus counters are expected to behave.

```console
$ python3 controller/runletd.py ... --metrics-port 9187
$ curl -s localhost:9187/metrics | grep -v '^#'
runlet_jobs{state="queued"} 0
runlet_jobs{state="running"} 1
runlet_jobs{state="done"} 4
runlet_queued_jobs 0
runlet_runners{kind="job"} 1
runlet_runners{kind="warm"} 0
runlet_job_running_seconds{repo="OWNER/REPO",job="smoke",runner="runlet-130317391252-1"} 12.0
runlet_runner_missing 0
runlet_reconcile_total 3
```

| Metric | Type | Meaning |
| --- | --- | --- |
| `runlet_jobs{state}` | gauge | Jobs in the ledger by state |
| `runlet_queued_jobs` | gauge | Jobs waiting for a runner |
| `runlet_queued_oldest_seconds` | gauge | Age of the longest wait |
| `runlet_job_wait_seconds{repo,job}` | gauge | Wait time per queued job |
| `runlet_runners{kind}` | gauge | Runners by kind, job or warm |
| `runlet_job_running_seconds{repo,job,runner}` | gauge | Elapsed time per running job |
| `runlet_runner_missing` | gauge | Active leases with no container |
| `runlet_job_attempts_total` | counter | Retries across all jobs |
| `runlet_reconcile_total` | counter | Passes since start |
| `runlet_reconcile_failures_total` | counter | Passes that raised |
| `runlet_reconcile_duration_seconds` | gauge | Duration of the last pass |
| `runlet_last_reconcile_timestamp_seconds` | gauge | When the last pass ran |

Two alerts worth having:

```yaml
- alert: RunletSchedulerStalled
  expr: time() - runlet_last_reconcile_timestamp_seconds > 120
  for: 5m
  annotations: { summary: "runletd has not reconciled in two minutes" }

- alert: RunletRunnerMissing
  expr: runlet_runner_missing > 0
  for: 5m
  annotations: { summary: "leases exist with no container behind them" }
```

`runlet_last_reconcile_timestamp_seconds` is the signal ARC lacks: it distinguishes a scheduler that is working from one that is still running but no longer receiving jobs.
