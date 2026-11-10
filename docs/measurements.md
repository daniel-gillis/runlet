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
| 1 | 835 ms |
| 2 | 873 ms |
| 3 | 868 ms |
| 4 | 858 ms |
| **p50** | **868 ms** |

(Two runs in the sample reported ~14 s; those are a log-line pairing artifact in
the harness — the API reports the whole job as 3–4 s — and are excluded pending a
fix that scopes the log window per run.)

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
