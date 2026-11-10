# Setup

Two ways to use this today:

- **As an image** — build (or pull) the runner image and run one container per job.
- **As a patch** — apply `patches/runlet-fast-path.patch` to your own
  `actions/runner` checkout and build it however you already build runners.

Be clear about what you are getting: **a faster agent, not a scheduler.** The image
runs exactly one job and exits, because a JIT-configured runner is single-use by
GitHub's design. Running a fleet means something must mint configurations and start
containers — that orchestration is not included yet.

---

## 1. Build the image

```console
$ docker buildx build \
    --platform linux/amd64 \
    -t ghcr.io/<you>/runlet-runner:dev \
    ./runner
```

This clones `actions/runner` at the pinned commit in `patches/UPSTREAM_COMMIT`,
applies the patch, and builds the agent from source. Expect 5–10 minutes on first
build.

The Dockerfile **fails the build** if the patch does not apply, so an image that
exists is an image that is patched.

## 2. Mint a single-use runner configuration

A JIT configuration is what removes registration from the cold path: no token
exchange, no long-lived credential on disk.

```console
$ gh api -X POST repos/<owner>/<repo>/actions/runners/generate-jitconfig \
    -f name=runlet-$(date +%s) \
    -F runner_group_id=1 \
    -f 'labels[]=runlet' \
    -f work_folder=_work \
  | jq -r .encoded_jit_config > /tmp/jit.txt
```

> **Labels replace the defaults.** Passing `labels[]=runlet` gives the runner
> *only* that label — `self-hosted` is not added automatically. Your workflow must
> then say `runs-on: [runlet]`, not `runs-on: [self-hosted, runlet]`.

## 3. Run one job

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    ghcr.io/<you>/runlet-runner:dev
```

The container registers, takes one job, and exits. For a workflow:

```yaml
jobs:
  build:
    runs-on: [runlet]
    steps:
      - uses: actions/checkout@v4
      - run: make test
```

`actions/checkout` and the JavaScript actions work because the agent bundles its
own Node runtime; Docker actions need the socket mount above.

## 4. Verify the patch is actually active

The patch adds perf counters. Enable them and run one job:

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -e RUNNER_PERFLOG=/tmp/perf \
    -v /tmp/perf:/tmp/perf \
    ghcr.io/<you>/runlet-runner:dev
$ grep -E "JobFetched|SentJobToWorker" /tmp/perf/Runner.perf
```

If `SentJobToWorker` appears **before** `JobRequestRenewed`, the fast path is
active. On an unpatched runner the order is reversed — that is the whole patch.

## 5. Or: apply the patch to your own build

```console
$ git clone https://github.com/actions/runner.git
$ cd runner && git checkout $(cat ../patches/UPSTREAM_COMMIT)
$ git apply /path/to/patches/runlet-fast-path.patch
$ cd src && ./dev.sh layout Release linux-x64
```

Requires .NET SDK 8.0.427 (the repo's `global.json` pins it; `dev.sh` downloads
its own copy).

---

## What the patch changes

| change | effect | measured |
|---|---|---|
| Dispatch before lock renewal | the worker starts without waiting for an HTTP round trip to GitHub | **1731 → 1470 ms p50**, max 2460 → 1559 ms |
| Acknowledge concurrent with job fetch | the best-effort acknowledge no longer blocks the fetch | 1451 → 1262 ms in the broker flow |
| Perf counters (`RefParsed`, `AckDone`, `JobFetched`) | lets you see where the time goes on your own runner | — |

Both are described in [`measurements.md`](measurements.md), including the
measurement bugs that produced wrong answers first — worth reading before you
trust any number, including these.

## What it does not change

- **Job execution.** Same agent, same steps, same logs, same isolation.
- **Queue time at GitHub.** ~3 s from "run created" to "job assigned" is not ours.
- **The job fetch.** ~700 ms of GitHub latency remains on the critical path; the
  patch overlaps what it can, not what it cannot.

## Requirements and limits

- Linux runners only (`linux/amd64` today; the patch is architecture-neutral).
- One job per container. No warm pool, no reuse.
- The image is ~400 MB: the agent layout plus a shell, git and archive tools.
  JavaScript actions use the agent's bundled Node; bring your own toolchain for
  `run:` steps that need Python, Go, etc.
- Not affiliated with GitHub. The agent is MIT; so is this patch.
