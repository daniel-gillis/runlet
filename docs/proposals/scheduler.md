# Proposal: lease-based job distribution

Status: design proposal. Nothing in this document is implemented. The shipped
controller polls the GitHub API and starts one container per queued job, which is
enough for the cases in [controller.md](../controller.md). This proposal covers what
would replace it if polling and per-job startup stopped being adequate.

## The problem with the current approach

The controller asks GitHub for queued jobs every few seconds and starts a container
for each one. That is simple and it scales to zero, but it has three limits:

- Dispatch latency is bounded by the poll interval, or by the job fetch inside the
  agent.
- A job that is visible but already claimed can still cause a wasted container.
- There is no queue-level view, so capacity cannot be prepared before work exists.

## What a better scheduler would need

Four properties, taken from systems that already solve this:

| Source | Property to keep |
| --- | --- |
| Kubernetes | Level-triggered reconciliation with periodic resync |
| Temporal, task queues | Leases with heartbeats, plus fencing tokens |
| NATS JetStream | Acknowledgment and redelivery |
| Buildkite agent | A dumb agent that only asks for work |

Missing events should cost one reconciliation cycle, never a lost job. That single
property removes the failure mode where a listener goes deaf and a job waits for
hours while runners sit idle.

## Participants

- Source adapter. Watches an upstream and records jobs. Holds no important state.
- Ledger. Durable record of every job and its lease history.
- Scheduler. Assigns jobs to runners. Stateless; anything it computes can be
  recomputed from the ledger.
- Runner. Holds at most one lease at a time and asks the scheduler for work.

## Messages

Four messages, each idempotent.

```
JobOffered    source -> ledger
  job_id, source, labels[], priority, created_at, payload_ref

LeaseRequest  runner -> scheduler
  runner_id, labels[], wait_timeout

LeaseGranted  scheduler -> runner
  job_id, lease_id, fencing_token, deadline, payload_ref   (or 204)

LeaseAck      runner -> scheduler
  lease_id, fencing_token, outcome, result_ref?
```

Rules:

1. `JobOffered` is a fact in the ledger. A scheduler that was offline for an hour
   finds the same queue on restart.
2. A lease has a deadline. If it expires, the job returns to the queue with a
   higher fencing token.
3. The fencing token is monotonic per job, so a paused or partitioned runner cannot
   acknowledge work it no longer owns.
4. `LeaseAck` is advisory. A lost acknowledgement means the lease expires and the
   job is retried, so delivery is at-least-once and runners must be restartable.
5. `payload_ref` is a pointer. Messages stay small regardless of job size.

## Reconciliation

Every participant runs the same loop on a jittered interval:

```
desired := ledger.jobs_in_state(queued)
active  := ledger.leases_in_state(active)
for job in desired where no active lease:  enqueue for dispatch
for lease in active where deadline passed: expire, return job to queued
for job in desired older than resync_floor: confirm it still exists upstream
```

This is why the orphan class disappears. There is no edge to miss: a missed event
produces the same outcome one cycle later.

## Latency target

| Stage | Target | Mechanism |
| --- | --- | --- |
| Job appears to offered | 50 to 500 ms | webhook, not polling |
| Offered to leased | under 50 ms | runner already waiting |
| Leased to executing | 0.2 to 0.6 s | pre-registered runner, small image |

The last row is what the patch and the image already deliver. The first two are
what a scheduler would change.

## Non-goals

- Replacing GitHub's runner protocol. Not possible; see
  [design.md](../design.md).
- Provisioning machines. Leases are the scheduler's unit; hardware is the
  cluster's.
- Interpreting workflows. Jobs are opaque.

## What would have to be measured

The proposal is only worth building if the latency it targets beats what the
controller already achieves. Before writing code, dispatch twenty jobs against a
warm pool and record the median and maximum from queue to first step. If the
current controller is within a few hundred milliseconds of the table above, the
scheduler adds complexity without changing what users experience.
