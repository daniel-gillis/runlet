<p align="center">
  <img src="assets/logo.svg" width="88" alt="runlet">
</p>

<h1 align="center">runlet</h1>

<p align="center"><em>Fast-start CI runners for GitHub Actions.</em></p>

<p align="center">
  <img alt="CI" src="https://github.com/daniel-gillis/runlet/actions/workflows/ci.yml/badge.svg">
  <img alt="status" src="https://img.shields.io/badge/status-working%20patch%20series-orange">
  <img alt="licence" src="https://img.shields.io/badge/licence-MIT-blue">
  <img alt="measured" src="https://img.shields.io/badge/assign%E2%86%92first%20step-1470ms%20p50-informational">
</p>

**Project site: [daniel-gillis.github.io/runlet](https://daniel-gillis.github.io/runlet/)**

---

## What this is

A patch series for the official GitHub Actions runner (MIT) that removes network
round trips from the start of every job, plus a container image built from it.

It is **not** a reimplementation. The agent must speak GitHub's protocol, so the
protocol is not ours to redesign, what is ours is *when the runner starts work*.

| | before | after |
|---|---|---|
| job assigned → first step (p50) | 1731 ms | **1470 ms** |
| same, p100 | 2460 ms | **1559 ms** |

Same machine, same job, three runs each. Details and the mistakes found on the way:
[`docs/performance.md`](docs/performance.md).

## What the patch actually does

Instrumenting the runner's own perf counters showed where a trivial job's time goes:

```
job message received
  → lock renewal (HTTP to GitHub)      2.4 s on a cluster node, 1.2 s locally  ← blocked dispatch
  → spawn Runner.Worker (new .NET process)
  → acknowledge (HTTP)                  133 ms                                 ← blocked the fetch
  → fetch job definition (HTTP)         697 ms                                 ← unavoidable
  → first step executes
```

Two of those waits were pure overhead:

1. **Dispatch no longer waits for the lock renewal.** The worker starts
   immediately; the renewal is enforced straight after hand-off, so a failed
   renewal still kills the worker before any step is reported.
2. **Acknowledge runs concurrently with the job fetch.** It is documented
   "best-effort" yet was awaited first.

What is left is GitHub's own latency: ~700 ms to fetch the job, plus ~3 s of queue
time before the runner is even told about it.

## Quickstart

```console
$ docker pull ghcr.io/daniel-gillis/runlet-runner:0.1.0   # or build it: see docs/setup.md
$ gh api -X POST repos/<owner>/<repo>/actions/runners/generate-jitconfig \
    -f name=runlet-$(date +%s) -F runner_group_id=1 \
    -f 'labels[]=runlet' -f work_folder=_work | jq -r .encoded_jit_config > /tmp/jit.txt
$ docker run --rm -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" runlet-runner
```

For a fleet instead of one job, run the controller:

```console
$ GITHUB_TOKEN=$(gh auth token) python3 controller/runletd.py \
    --repo <owner>/<repo> --serve runlet-runner --image runlet-runner --max 8
```

Then target it from a workflow with `runs-on: [runlet]`.

Full instructions, including how to verify the patch is active on your own runner:
**[`docs/setup.md`](docs/setup.md)**.

## Repository map

| path | what |
|---|---|
| [`patches/`](patches/) | the patch series, pinned to an upstream commit |
| [`runner/`](runner/Dockerfile) | multi-stage image: builds the patched agent, packages a runtime |
| [`controller/`](controller/runletd.py) | scale-from-zero controller: queued job → mint → container |
| [`docs/setup.md`](docs/setup.md) | build, mint, run, verify |
| [`docs/controller.md`](docs/controller.md) | how the controller works and where it stops |
| [`docs/performance.md`](docs/performance.md) | every number, including the wrong ones |
| [`docs/agent-protocol.md`](docs/agent-protocol.md) | what the runner protocol is, verified from a live config |
| [`docs/design.md`](docs/design.md) | why this is a patch rather than a new agent |
| [`tools/`](tools/) | the measurement harness and the action-compatibility sweep |
| [`agent/`](agent/) | Rust JIT-config decoder (groundwork for a future agent) |
| [`PROTOCOL.md`](PROTOCOL.md) | lease-based scheduling design for the not-yet-built scheduler |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | component design, latency budget, open questions |

## Honest limits

- **One job per container, by design.** A JIT-configured runner is single-use.
  Running a fleet is what [`controller/runletd.py`](controller/runletd.py) is for:
  it watches queued jobs, mints a config per job, starts a container, and lets it
  exit. Verified scale-from-zero and back to zero, 
  see [`docs/controller.md`](docs/controller.md).
- **~700 ms of GitHub latency remains** on the critical path. The patch overlaps
  what it can; it cannot remove a fetch.
- **~3 s of queue time** before the runner hears about a job. Not ours.
- **Linux only** today.
- Every measurement in this repo was produced by a harness that had three separate
  bugs which each made a wrong result look plausible. Read
  [`measurements.md`](docs/measurements.md) §9–11 before trusting any of it,
  including the headline.

## Compatibility

`tools/compat_sweep.py` reads the `runs.using` field of the most-used actions:
**24 of 25 declare `node20`**, which the agent bundles. The `setup-*` actions
download their toolchains at runtime, so nothing needs to be baked in.
Full table: [`docs/compatibility.md`](docs/compatibility.md).

## Licence

MIT. Not affiliated with GitHub, Inc. The runner agent is MIT and is used
unmodified apart from the patch in `patches/`.
