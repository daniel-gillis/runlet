<p align="center">
  <img src="assets/logo.svg" width="88" alt="runlet">
</p>

<h1 align="center">runlet</h1>

<p align="center">Fast-start runners for GitHub Actions, built as a patch to the official agent.</p>

<p align="center">
  <a href="https://github.com/daniel-gillis/runlet/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/daniel-gillis/runlet/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="cold start" src="https://img.shields.io/badge/assign%20to%20first%20step-1470ms%20p50-informational">
</p>

A GitHub Actions runner spends about a second and a half on paperwork before the first step runs. Two of those steps are HTTP calls to GitHub that the agent makes in series. Both can be moved off the critical path.

runlet is a patch series for the official runner (MIT licensed) that does that, plus a container image built from it and a controller that starts runners when jobs appear.

| Job assigned to first step | Baseline | Patched |
| --- | --- | --- |
| Median | 1731 ms | 1470 ms |
| Maximum | 2460 ms | 1559 ms |

Three runs per variant, one host, one job. Method and raw data are in [docs/performance.md](docs/performance.md).

## Install

```console
$ docker pull ghcr.io/daniel-gillis/runlet-runner:0.1.0

$ gh api -X POST repos/OWNER/REPO/actions/runners/generate-jitconfig \
    -f name=runlet-$(date +%s) -F runner_group_id=1 \
    -f 'labels[]=runlet-runner' -f work_folder=_work \
  | jq -r .encoded_jit_config > /tmp/jit.txt

$ docker run --rm -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    ghcr.io/daniel-gillis/runlet-runner:0.1.0
```

The runner registers, takes one job, and exits. Point a workflow at it with `runs-on: [runlet-runner]`. A JIT configuration's label list replaces the defaults, so adding `self-hosted` to `runs-on` will match nothing.

To serve more than one job, run the controller instead of a single container:

```console
$ GITHUB_TOKEN=$(gh auth token) python3 controller/runletd.py \
    --repo OWNER/REPO --serve runlet-runner \
    --image ghcr.io/daniel-gillis/runlet-runner:0.1.0 --min-warm 2 --max 8
```

## What the patch changes

`JobDispatcher` waits for a lock renewal, an HTTP call to GitHub, before it starts the worker process. The patch starts the worker first and enforces the renewal right after hand-off, so a failed renewal still stops the worker before any step reports.

The broker flow acknowledges a job and then fetches it, in series. The patch runs both at once.

The patch touches two files in `actions/runner`. CI applies it to the pinned upstream commit and fails if it touches anything else.

## Documentation

| | |
| --- | --- |
| [Setup](docs/setup.md) | Build, mint a configuration, run, confirm the patch is active |
| [Controller](docs/controller.md) | Scaling, warm pools, webhooks, per-label images |
| [Performance](docs/performance.md) | Every number and how it was produced |
| [Design](docs/design.md) | Why this is a patch rather than a new agent |
| [Troubleshooting](docs/troubleshooting.md) | Job stays queued, container exits, build fails |
| [FAQ](docs/faq.md) | Common questions |
| [Architecture](docs/architecture.md) | Components, latency budget, open questions |
| [Runner protocol](docs/runner-protocol.md) | The agent's wire protocol, verified against a live configuration |
| [Scheduler](docs/scheduler.md) | Ledger, leases, fencing, retries, and the status command |
| [Changelog](CHANGELOG.md) | Release history |

## Limitations

- GitHub takes 1 to 2 seconds to turn a trigger into a queued job. A waiting runner picks it up immediately; nothing here changes that interval.
- Roughly 700 ms of job-fetch latency stays on the critical path. The patch overlaps what it can.
- A JIT runner serves one job. Reusing a runner would need token registration and would carry state between jobs.
- Linux only, amd64 and arm64.
- GitHub Enterprise Server is untested.

## License

MIT. Not affiliated with GitHub, Inc. The runner agent is MIT licensed and is used unmodified apart from the patch series.
