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
22:54:49  dispatching daniel-gillis/homelab "smoke" to runlet-130310446675-1 (labels ['runlet-runner'], attempt 1)
22:55:36  finished daniel-gillis/homelab "smoke" after 49s (runlet-130310446675-1)
```

Failures name the reason rather than an error code:

```
retrying daniel-gillis/homelab "build" (attempt 2 of 3): the runner exited without taking the job
giving up on daniel-gillis/homelab "build" after 3 attempts: the runner exited without taking the job
cancelled daniel-gillis/homelab "deploy": it is no longer queued upstream
```

### Status

```console
$ python3 controller/runletd.py status --state runlet-ledger.db --max 8
RUNNING 2 of 8
       6s  integration tests                          daniel-gillis/homelab   attempt 2
    3m12s  build                                    daniel-gillis/homelab

QUEUED 1
      45s  deploy                                   daniel-gillis/homelab   runlet-runner

RECENT
       0s ago  smoke                                daniel-gillis/homelab   ok, took 48s
     9m ago  lint                                   daniel-gillis/homelab   ok, took 22s
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
