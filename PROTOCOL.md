# Runlet protocol, draft 0.1

A **level-triggered, lease-based job distribution protocol** for CI runners.

Status: design draft. Nothing here is implemented yet; this document exists to
make the invariants falsifiable.

---

## 1. The boundary problem (read this first)

There are two protocols in a self-hosted GitHub Actions setup, and **both are
owned by GitHub**:

| Hop | Owner | Can we replace it? |
|---|---|---|
| runner ⇄ `github.com` (job fetch, logs, artifacts, cache, results) | GitHub | **No.** The runner must speak their wire format or it receives no jobs. |
| controller ⇄ `broker.actions.githubusercontent.com` (sessions, `JobAvailable`, `AcquireJobs`) | GitHub | **No.** Same reason. |
| **"a job exists" → "a runner is running it"** (scheduler, queue, assignment, lifecycle) | **us** | Yes, this is the whole design space. |

So a new protocol cannot replace GitHub's. It can replace the layer that
*consumes* GitHub's, and, if the layer is written behind an adapter, it can
consume GitLab's, Gitea's, or a plain webhook the same way.

"Infinitely extensible" is therefore not a claim about the wire format. It is a
claim about **the seam**: one durable scheduling core, adapters on both sides
(downstream: where jobs come from; upstream: where jobs run). That seam is what
this document specifies.

## 2. What the existing protocols get right, and wrong

| System | Best piece | Shortfall |
|---|---|---|
| **ARC / `actions/scaleset`** | ephemeral runners; JIT registration; scale-to-zero | **edge-triggered assignment**: a queued job is announced once via `JobAvailable`; if the listener misses it, the job waits forever with idle runners. No re-sweep on session start. |
| **Kubernetes** | **level-triggered reconciliation** with periodic resync; informers; no missed-state class | assumes a watch channel and a versioned store; heavy to adopt for a single queue |
| **Buildkite agent** | agent-side pull loop; trivially robust; no central dispatcher to fail | latency bound by poll interval; central token sprawl |
| **GitLab Runner** | long-poll (`/jobs/request`) with concurrency; proven at scale | bespoke, undocumented wire format; monolithic coordinator |
| **Temporal / task queues** | durable state, **leases with heartbeat**, at-least-once + idempotency keys | heavyweight runtime; more machinery than a CI queue needs |
| **NATS JetStream work-queue** | ack/redelivery, max-inflight, durable consumer | external dependency; no notion of a *runner* |

**Synthesis:** take Kubernetes' level-triggered resync, Temporal's lease +
fencing, JetStream's ack/redelivery, and Buildkite's dumb-agent simplicity; take
*no* part of edge-triggered one-shot delivery.

## 3. Participants

- **Source adapter**, observes an upstream (GitHub, GitLab, webhook) and emits
  `JobOffered`. Owns no state that matters; may be restarted at will.
- **Ledger**, durable, append-only record of every job and its lease history.
  Single source of truth. Losing an adapter must never lose a job.
- **Scheduler**, leases jobs to runners. Stateless; anything it computes can be
  recomputed from the ledger.
- **Runner**, a long-lived, pre-registered worker that holds at most one
  *active* lease. Long-polls the scheduler; never asks GitHub for work.

## 4. Messages

Only four. Every one is idempotent by construction.

```
JobOffered      source → ledger
  { job_id (global, immutable), source, repo, labels[], priority, created_at,
    payload_ref }                       // payload lives in object storage, not in the message

LeaseRequest    runner → scheduler
  { runner_id, labels[], capacity=1, wait_timeout }   // long-poll, capped at 25s

LeaseGranted    scheduler → runner
  { job_id, lease_id, fencing_token, deadline, payload_ref }   // or 204 No Content

LeaseAck        runner → scheduler
  { lease_id, fencing_token, outcome: accepted|rejected|completed|failed,
    result_ref?, reason? }
```

Invariants:

1. **Nothing is announced once.** `JobOffered` is a fact in the ledger; a
   scheduler that was offline for an hour finds the same jobs on restart by
   reading the queue. There is no message to miss.
2. **Assignment is a lease, not a handoff.** A lease has a deadline; if no
   heartbeat arrives before it, the job returns to the queue and is offered
   again with a **higher fencing token**.
3. **The fencing token is monotonic per job.** A runner that was paused
   (`SIGSTOP`), partitioned, or resumed late finds its token stale and must
   abandon its work. Two runners can never both believe they own a job.
4. **`LeaseAck` is advisory.** If the ack is lost, the lease simply expires and
   the job is retried. Delivery is at-least-once end to end; runners must be
   restartable, which CI jobs already are.
5. **`payload_ref` is a pointer, never inline.** The message stays small and
   constant-time to parse; big payloads live in the object store with their own
   lifecycle.

## 5. Anti-entropy: why the orphan class cannot exist

Every participant runs the same loop, on a jittered interval:

```
reconcile():
  desired  := ledger.jobs_in_state(queued)            # level, not events
  actual   := ledger.leases_in_state(active)
  for job in desired where no active lease:
      enqueue_for_dispatch(job)                       # idempotent
  for lease in actual where deadline passed or heartbeat stale:
      ledger.expire(lease)                            # returns job to queued
  for job in ledger.jobs_in_state(queued) older than resync_floor:
      source_adapter.confirm_still_exists(job)        # upstream may have cancelled it
```

This is the single most important property of the design. ARC's worst bug, 
"listener went deaf, job queued for hours while runners sat idle", is not a bug
that can be fixed here; it is a **shape that does not exist**, because there is
no edge to miss.

Contrast the two shapes explicitly:

- **edge-triggered**: `on JobAvailable → acquire()`. Missed event ⇒ lost job.
- **level-triggered**: `every tick → queued set vs active set → converge`. Missed
  event ⇒ identical outcome one tick later.

## 6. Cold-start budget

**Target: p99 ≤ 3 s from "job queued at the source" to "first step executing",
starting from zero runners.** No warm pool: capacity exists only while work does.

That target is only reachable if *every* stage below is optimised. Any one stage
left at its default blows the budget on its own, which is why this table is the
design rather than an afterthought.

| # | Stage | Default | Optimised | Mechanism |
|---|---|---|---|---|
| 1 | queued → adapter notices | 2–30 s | **0.05–0.5 s** | 250 ms poll, or webhook; must not depend on a broker long-poll |
| 2 | runner allocated → image materialised | 5–60 s | **0.1–0.4 s** | lazy-pull (nydus/stargz) or snapshot restore; never a full layer pull |
| 3 | container/microVM start | 0.1–0.3 s | **0.05–0.15 s** | microVM snapshot restore, or runc + tiny rootfs |
| 4 | registration | 0.3–1.0 s | **0 s** | JIT config pre-minted in bulk, no GitHub API call on the critical path |
| 5 | registered → job received | 0.1–0.5 s | 0.1–0.5 s | long-poll; unavoidable upstream floor |
| 6 | workspace ready | 1–20 s | **0.1–0.5 s** | node-local git mirror + overlay, not a cold clone |

Stage 4 is the one people miss. ARC generates a JIT runner config *after* the
pod exists, so every cold start pays a round-trip to `api.github.com`. Minting
configs ahead of demand (they are cheap, single-use and short-lived) removes that
entire stage from the critical path.

Stage 6 is the second one people miss: a cold `git clone` of a real repository is
consistently slower than every other stage combined. A node-local bare mirror
plus an overlay checkout turns it into a local operation.

## 7. Making the runner small enough to start fast

"Optimise the runner from the ground up" is the right instinct, and it is
specific: **852 MB is not the agent, it is the distro.**

| Component | Typical | Achievable | How |
|---|---|---|---|
| `actions/runner` agent (.NET) | ~200 MB + runtime | **60–90 MB** | the agent is MIT, build it trimmed/AOT instead of taking the release tarball |
| Base OS | 300–500 MB Debian | **2–10 MB** | distroless/static; there is no shell to need |
| Toolchain (node, python, docker CLI) | 300–600 MB | **0 MB in the base** | label-driven read-only overlay images, mounted only when a job asks |

The third row is the structural idea: **do not bake a toolchain into the runner.**
A job declares `runs-on: [linux, node20]`; the executor overlays a node image
read-only. The base runner stays small, so cold start only materialises what the
job's labels require, a job that needs only `git` never pays for Node.

Consequence for the protocol: a runner advertises **capabilities**, and the
scheduler refuses to place a job on a runner whose overlay set cannot satisfy its
labels. Capability matching is part of the lease, not an afterthought.

## 7a. Executor profiles

| Profile | Isolation | Cold start | Notes |
|---|---|---|---|
| `runc-tiny` | container | 0.5–1.5 s | stock k8s, lazy-pull, distroless base. **Start here.** |
| `microvm` | hardware-virtualised | 0.2–0.6 s | Firecracker + snapshot pool; needs KVM; strongest boundary |
| `criu` | container (restored) | 0.1–0.3 s | fastest on paper, most fragile in practice; not recommended |

All three implement the same `LeaseRequest`/`LeaseAck` surface; the protocol does
not care which is underneath.

## 8. Extensibility: adapters, not forks

```
  GitHub Actions ─┐
  GitLab CI ──────┼──▶ [source adapter] ──▶ ledger ──▶ scheduler ──▶ runner ──▶ [executor adapter] ──▶ k8s / microVM / VM / bare metal
  Gitea ──────────┘                                                        └──▶ [result adapter] ──▶ GitHub logs/artifacts
  webhook ────────┘
```

Adding a source = translating its events to `JobOffered`. Adding an executor =
implementing `LeaseRequest`/`LeaseAck` against it. Neither touches the ledger or
the scheduler. This is the actual extensibility claim, and it is testable: a
source adapter must pass a conformance suite that replays a trace of upstream
events and asserts the resulting ledger state.

## 9. Failure model (what the protocol refuses to do)

| Failure | Behaviour |
|---|---|
| Scheduler dies | Runners' long-polls fail; leases expire; nothing is lost. New scheduler reads the ledger. |
| Runner dies mid-job | Lease expires; job re-queued with a higher fencing token; the zombie's acks are rejected. |
| Ledger unavailable | Scheduler serves nothing. Explicitly **not** degraded to an in-memory queue, failing closed is the point. |
| Source adapter down | Jobs keep running; new jobs are not discovered. No orphan class, because nothing was edge-triggered. |
| Duplicate `JobOffered` | Deduplicated on `job_id`; idempotent. |
| Clock skew | Deadlines are lease-relative (`deadline = grant_time + ttl`), never wall-clock comparisons between machines. |

## 10. Non-goals

- Replacing GitHub's runner wire protocol (impossible, §1).
- Being a general workflow engine. Jobs are opaque: we move them, we don't
  interpret them.
- Autoscaling nodes. We scale *leases*; node provisioning is the executor's
  problem.

## 11. Falsification plan

The design is only interesting if the latency claim holds. Measure, before
building more:

1. Stand up ARC in the target cluster; run 20 jobs; record p50/p95/p99
   queued→running. (Expect 30–60 s.)
2. Implement §4 + §5 with a single source adapter and `reuse-scrubbed`; same 20
   jobs; same measurement.
3. Kill the scheduler mid-run and the runner mid-job; assert zero orphans and
   zero double-executions.
4. If (2) is not ≥10× better than (1), the thesis is wrong and the project
   should stop.
