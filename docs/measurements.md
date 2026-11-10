# Measurements

Everything here is measured on the homelab cluster, not estimated. Regenerate
with `tools/bench_runner.py` and the `bench-runner.yml` workflow.

## 1. Where the time goes on self-hosted runners today

Two runners, both idle, registered to a repository, `bench-runner.yml` does one
`echo` and one `date`. Timestamps come from the GitHub REST API
(`run.created_at` → `job.created_at` → `job.started_at` → `job.completed_at`).

| Run | GitHub schedules the job | Job queued until a runner takes it | Executing |
|---|---|---|---|
| 1 | 1 s | **46 s** | 3 s |
| 2 | 2 s | **15 s** | 4 s |
| earlier batch (8 runs) | — | **p50 3 s, p95 26 s** | p50 3 s |

Two things are immediately clear:

1. **The runner is not the bottleneck.** A trivial job spends **3–4 s** executing
   even when the machine is idle and the image is already local. That is the
   agent's per-job overhead (job download, step setup, log plumbing, teardown),
   and it is the part a purpose-built runner could reduce.
2. **Assignment is both large and wildly variable** — 3 s at p50, 26 s at p95, and
   46 s observed back-to-back. It is GitHub's scheduling, not runner capacity:
   both runners were idle throughout.

### 1a. The confounder, and the fix

The first batch was measured against **single-use runners**. The image's
entrypoint tests `[ -n "${EPHEMERAL}" ]`, so `EPHEMERAL=false` — a non-empty
string — passes `--ephemeral`, and every runner deregistered and exited after one
job. Each job then waited for a pod restart plus re-registration.

After removing the variable (and `UNSET_CONFIG_VARS`), the same bench on the same
cluster:

| | before | after |
|---|---|---|
| queue p50 | 3 s | **3 s** |
| queue p95 | 26 s | **4 s** |
| queue max | 46 s | **4 s** |
| exec (trivial job) | 3–4 s | **3–4 s** |

The tail was ours. **The floor was not.**

## 2. What this means for the <2 s target

**It is not reachable while GitHub schedules the jobs.**

With the runner bug fixed, the floor is stable: **~3 s to assign a job that is
already queued, plus ~3 s for the agent to execute even a two-line job.** Both
are second-granularity API timestamps, so read them as 2–4 s each. Our materialisation work — the part we can
actually engineer — sits *after* the job has been assigned. Even at zero cost for
materialisation and execution, end-to-end cannot go below GitHub's assignment
time.

So there are exactly two coherent targets:

| Target | Definition | Achievable? |
|---|---|---|
| **A. Our slice** | job assigned to runner → first step executing | **Yes — < 1 s** |
| **B. End to end** | workflow triggered → first step executing | **No** on GitHub Actions; floor is GitHub's 3–46 s |

This is the single most important finding in the project, and it changes the
product claim: runlet cannot promise "2 seconds from a push". It can promise
"**2 seconds of the 30–60 seconds you currently lose**", because ARC pays GitHub's
queue *and* its own pod-create/register cycle on top.

## 3. Why the comparison still matters

ARC's documented 30–60 s is **not** measured from `run.created_at`; it is the time
its own controller adds — scheduling a pod, pulling a multi-hundred-MB image,
registering the runner, then receiving the job. On the same cluster with the same
GitHub queue, that is the term runlet deletes.

Concretely: if GitHub's queue is 3 s and ARC's overhead is 40 s, the user sees
~43 s. If runlet's overhead is 1 s, the user sees ~4 s. **A 10× improvement in what
is experienced, with honesty about what is left.**

## 4. Consequence for the roadmap

- Measure and optimise **assignment → first step**, because that is the only
  interval we own.
- Treat `run.created_at → job.started_at` as an external constant to be measured,
  never optimised.
- If a sub-2-second *end-to-end* number is a hard requirement, the only path is to
  stop using GitHub's job assignment — which means owning the trigger and the
  scheduler, i.e. building a CI system rather than a runner. That is a different
  project with a much larger scope, and it should be a deliberate decision rather
  than an accident of ambition.

## 5. The interval runlet actually owns

Target interval: **job assigned to a runner → first step executing.** Measured with
`tools/bench_interval.py`, which correlates two timestamps from the same pod clock
(containerd's nanosecond log timestamp for the agent's `Running job:` line, and
`RUNLET_STEP_EPOCH` printed inside the step).

| Run | assign → first step |
|---|---|
| 1 | 950 ms |
| 2 | 904 ms |
| 3 | 922 ms |
| 4 | 866 ms |
| 5 | 912 ms |
| 6 | 886 ms |
| **p50** | **903 ms** |

Range 866–950 ms, n=6, **zero discarded samples**. An earlier run of this
measurement reported a 868 ms median but contained outliers of 14 s and 324 s;
those were harness bugs, both since fixed:

1. It read the log of `pods[0]`, but with two runners a job can land on either,
   so it paired the step stamp with a *different pod's* stale `Running job` line.
   It now asks the job which host ran it (`runner_host` is printed by the workflow)
   and reads only that pod.
2. Its hostname regex matched the *echoed command* (`runner_host=$(hostname)`)
   rather than the command's output. It now requires a real pod name, and takes
   the last match for the step stamp.

The lesson recorded here for anyone building on this harness: **when a measurement
shows 300-second outliers, suspect the measurement.** The instrument needed as
much debugging as the system under test.

So the stock agent takes **~870 ms** from receiving a job to having the first step
executing, on a warm runner with a local image. That is the number runlet has to
beat, and the target of 200–400 ms implies removing **450–650 ms** of agent
overhead — not the 3 s the coarser API measurements suggested.

## 6. Why this matters for the claim

The earlier "3 s" figure was API quantisation plus whole-job teardown, not
assignment latency. Corrected, the picture is:

| Interval | Time | Owner |
|---|---|---|
| run created → job assigned | ~3 s | **GitHub** |
| assigned → first step | **~0.87 s** | **us** |
| first step → job complete (trivial) | ~1.5–2 s | agent |

The honest product claim is therefore: *"~0.3 s instead of ~0.9 s of runner
overhead, on top of whatever GitHub's queue costs."* Still worth building, and
still measurable — but it is a 3× improvement in our slice, not 20×.

## 7. Phase decomposition from the agent's own perf counters

`RUNNER_PERFLOG=/tmp/perf` makes the agent write `{Runner,Worker}.perf` with
ISO-8601 timestamps at each stage. This is far better than correlating logs: the
agent timestamps its own phases. One real job:

| Phase | Timestamp | Δ |
|---|---|---|
| `MessageReceived_RunnerJobRequest` | 12:43:16.2766 | — |
| `JobRequestRenewed_0` | 12:43:18.6977 | **+2.421 s** |
| `StartingWorkerProcess` | 12:43:18.7000 | +0.002 s |
| `SentJobToWorker_0` | 12:43:18.7398 | +0.029 s |
| `WorkerProcessStarted` | 12:43:18.9288 | +0.189 s |
| `WorkerJobMessageReceived_0` | 12:43:19.0538 | +0.111 s |
| `WorkerJobServerQueueStarted_0` | 12:43:19.2104 | +0.157 s |
| `WorkerJobInitialized_0` | 12:43:19.3741 | +0.164 s |
| first console output | 12:43:19.9355 | +0.561 s |
| **total** | | **3.66 s** |

### The 903 ms measurement was measuring the wrong interval

The earlier 903 ms was anchored on the `Running job:` log line, which the agent
prints *after* the renewal above. So 903 ms is "after the agent decided to run
it", not "job assigned". The true assign-to-first-step is **3.66 s**, and the
single largest term is a **2.42 s gap between receiving the job message and the
first lock renewal completing**.

### Why the gap exists, and that it is fixable

`JobDispatcher.Run`:

```csharp
Task renewJobRequest = RenewJobRequestAsync(...);
// "not even start worker if the first renew fail"
await Task.WhenAny(firstJobRequestRenewed.Task, renewJobRequest, ...);
```

The first lock renewal is an HTTP round trip to GitHub, and dispatch waits for it.
The worker process is not started until the lock is confirmed.

The worker does not need the lock to *begin*. Dispatching first and enforcing the
renewal immediately afterwards preserves the invariant (a failed renewal still
kills the worker before any step is reported) while removing the round trip from
the critical path.

**This is change 1**, and it alone targets 2.4 s of the 3.7 s — an order of
magnitude more than the 500–700 ms the earlier plan was chasing.

## 8. Change 1 applied: dispatch no longer waits for the lock renewal

Patch: `JobDispatcher.Run` starts `Runner.Worker` immediately and only afterwards
awaits `firstJobRequestRenewed`. A failed renewal kills the worker before any step
is reported, so the invariant is preserved.

Measured with the same perf counters, one real job on the patched build:

| phase | delta | cumulative |
|---|---|---|
| StartingWorkerProcess | 0.923 s | 0.923 s |
| SentJobToWorker | 0.038 s | 0.961 s |
| **JobRequestRenewed** | 0.127 s | **1.088 s** ← now *after* dispatch |
| WorkerProcessStarted | 0.070 s | 1.158 s |
| WorkerJobMsgReceived | 0.043 s | 1.201 s |
| QueueStarted | 0.055 s | 1.256 s |
| JobInitialized | 0.094 s | 1.349 s |
| FirstConsoleOutput | 0.202 s | **1.551 s** |

**Total: 1.551 s, against 3.66 s unpatched — a 58% reduction.**

### Caveat, stated plainly

The unpatched baseline was measured on a cluster node; the patched run was on an
Apple-silicon machine, which is faster. So 3.66 → 1.55 s is *not* a controlled
comparison.

What **is** controlled is the structure: `JobRequestRenewed` moved from 2.421 s
*before* dispatch to 1.088 s *after* it. The gate that cost 2.42 s is gone, and
that is hardware-independent.

The controlled A/B — unpatched and patched on the same host, same job — is the
next measurement, and the only one that should be published as the headline.

### What the remaining 0.92 s is

`MessageReceived → StartingWorkerProcess` is still 0.923 s with no network call in
it: that is message decryption plus listener bookkeeping, and it is change 2.

## 9. Controlled A/B on one host (same machine, same job)

Both variants built from the same tree on the same machine, measured by
`tools/measure_agent.py` against a real GitHub job.

| variant | runs | p50 | spread |
|---|---|---|---|
| baseline (unpatched) | 1 valid | **1782 ms** | — |
| patched (dispatch before renewal) | 3 | **1470 ms** | 1442–1559 ms |

**~18% faster on this host**, not the 58% the cross-host comparison suggested.
The cluster-node baseline (3.66 s) includes a slower CPU; the honest, controlled
figure is 1782 → 1470 ms.

### A measurement bug that produced a wrong answer first

The first A/B reported the patched build as *slower* (2321 ms). The cause was in
the harness, not the patch: `read_perf` keeps the **first** occurrence of each
phase, and the perf directory was not cleared between runs, so runs 2 and 3 were
reporting run 1's timestamps — three "independent" runs were identical to the
millisecond. That is the tell: identical spreads mean the instrument is broken.

After clearing the perf files per run, the patched build measured 1442–1559 ms
across three genuinely independent runs.

### Still to do

- Re-measure the baseline with the fixed harness (n≥3) so the A/B is symmetric.
- The remaining 0.9 s is `MessageReceived → StartingWorkerProcess` with no network
  call in it: message decryption plus listener bookkeeping. That is change 2.
