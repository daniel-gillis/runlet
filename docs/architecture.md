# Architecture

Three pieces ship today: a patch, an image built from it, and a controller. Nothing else is required.

```
GitHub ──(queued job)──▶ runletd ──(mint JIT config)──▶ docker run ──▶ agent ──▶ job
                            │                                            │
                            └── counts containers to derive capacity ◀───┘
```

## Components

**The patch.** Two changes to `actions/runner`: dispatch without waiting for the lock renewal, and acknowledge concurrently with the job fetch. Applied to a pinned upstream commit at build time.

**The image.** Multi-stage build. The first stage clones upstream, applies the patch, and compiles the agent. The second copies the agent layout onto Debian slim with git and archive tools. One job per container.

**The controller.** A polling loop over the GitHub API. It reads queued jobs, mints a single-use configuration per job, and starts a container. Capacity comes from counting running containers, so the controller holds no state between passes.

## Latency budget

Measured on one host with a warm runner:

| Stage | Time | Owner |
| --- | --- | --- |
| Queued to a runner being told | about 3 s | GitHub |
| Message received to worker started | about 900 ms | Agent, partly overlapped by the patch |
| Job definition fetched | 700 ms | GitHub |
| Worker started to first output | 200 to 500 ms | Agent and step |
| Configuration minted and container started | 1 to 3 s | Controller, removed by `--min-warm` |

The patch and warm pools act on the last two rows. The first and third belong to GitHub.

## Scaling model

One job per container. A JIT runner is single-use because the server marks it `Ephemeral`, so reuse would need token registration and state handling between jobs.

`--min-warm` trades idle memory for dispatch latency. `--max` caps concurrency so a queue spike cannot exhaust the host. Between them, the controller covers both burst and always-on patterns without a scheduler.

## Design limits

- The controller polls. A webhook shortens the path but adds a public endpoint and a shared secret.
- No queue-latency signal is available from the API, so capacity cannot grow before work appears.
- Jobs run in a container with the toolchain the image provides. Toolchain diversity is handled by label-to-image mapping, not by a single universal image.

## Reconciliation

The controller keeps a ledger of jobs and leases and reconciles it against the runtime on every pass, so a restart resumes and a failed container is retried rather than forgotten. [scheduler.md](scheduler.md) documents the rules.
