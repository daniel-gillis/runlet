# The controller

A JIT-configured runner serves exactly one job. That makes the image fast but
useless as a fleet on its own — something has to mint configurations and start
containers. `controller/runletd.py` is that something, and it is deliberately
small.

## What it does

```
poll: queued jobs whose labels we serve
  ↓
capacity = max - running containers
  ↓
for each queued job, up to capacity:
    mint a single-use JIT config with that job's labels
    docker run -d --rm <image> with the config in an env var
  ↓
the container takes the job, exits, disappears
```

## Verified behaviour

```
$ python3 controller/runletd.py --repo daniel-gillis/homelab \
    --serve runlet-runner --image runlet-runner:dev --max 2 --once
runletd: serving daniel-gillis/homelab for labels ['runlet-runner'] (max 2, ...)
queued=2 active=0 capacity=2
  started runlet-130077768121 for job 'smoke' labels=['runlet-runner']
  started runlet-130077752578 for job 'smoke' labels=['runlet-runner']
```

Both jobs then ran and **both containers exited**. `docker ps` afterwards: zero.
Scale from zero, and back to zero, with no state kept between passes.

## Usage

```console
$ export GITHUB_TOKEN=...            # repo scope is enough
$ python3 controller/runletd.py \
    --repo daniel-gillis/homelab \
    --serve runlet-runner \
    --image ghcr.io/you/runlet-runner:0.1.0 \
    --max 8 \
    --interval 5
```

| flag | meaning |
|---|---|
| `--repo` | `owner/repo` to serve |
| `--image` | runner image to start |
| `--serve` | a label we accept; repeatable, and a job is served only if **all** of its labels are covered |
| `--max` | hard cap on concurrent runners, so a queue spike cannot exhaust the host |
| `--interval` | poll period, seconds |
| `--network` | docker network to attach runners to |
| `--once` | one pass, for testing |
| `--dry-run` | log decisions, start nothing |

## Design choices worth knowing

**Matching is exact, and that is deliberate.** A JIT config's label list
*replaces* the defaults — a runner minted with `["runlet-runner"]` does not also
have `self-hosted`. So the controller mints each runner with the *job's own*
labels, which guarantees a match rather than hoping one exists.

**Containers are the only state.** There is no desired-state store, no database, no
reconciliation loop beyond "count what is running". Restart the controller and it
re-derives everything on the next pass. Losing the controller mid-queue costs at
most one poll interval.

**Failure is contained.** A failed mint or a failed `docker run` is logged and the
loop continues; one bad job cannot stop scaling for the rest.

**It does not wait for jobs to finish.** Each container is `--rm`, so capacity is
reclaimed by definition when it exits. The controller only counts.

## Running it as a service

```ini
[Unit]
Description=runletd — scale-from-zero controller for runlet runners
After=network-online.target docker.service
Wants=network-online.target

[Service]
Environment=GITHUB_TOKEN_FILE=/etc/runletd/token
ExecStart=/usr/bin/python3 /opt/runletd/controller/runletd.py \
  --repo daniel-gillis/homelab --serve runlet-runner \
  --image ghcr.io/you/runlet-runner:0.1.0 --max 8
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run

[Install]
WantedBy=multi-user.target
```

## Limits, stated plainly

- **One repository per process.** Multi-repo means running several instances; the
  token scope is the natural boundary.
- **Polling, not webhooks.** The default 5 s interval bounds how long a queued job
  waits for a runner. Webhooks would cut that; they need a public endpoint and a
  shared secret, which is a different deployment shape.
- **No queue-depth awareness beyond the API.** GitHub exposes queued runs, not
  queue latency, so the controller cannot pre-emptively warm capacity.
- **No per-profile images yet.** `--serve` accepts labels, but every runner uses
  one image. Multiple toolchain profiles would mean a label-to-image map.

## Working around the four limits

Each limit in the section above now has a working answer.

### 1. One repository per process → `--repo` is repeatable

```console
$ python3 controller/runletd.py --repo owner/a --repo owner/b --serve runlet-runner ...
```

One process, one token, one loop over multiple repositories.

### 2. Polling latency → `--webhook-port`

Polling bounds dispatch latency to the poll interval. With a webhook the loop
wakes the moment GitHub reports a `workflow_job` that is `queued`:

```console
$ export RUNLETD_WEBHOOK_SECRET=...        # same secret as the GitHub webhook
$ python3 controller/runletd.py --webhook-port 8787 ...
```

Signatures are verified with HMAC-SHA256 (`X-Hub-Signature-256`); unverified
requests get a 401 and are ignored. The poll interval stays as a safety net — a
webhook is an optimisation, not a dependency.

### 3. No pre-warming → `--min-warm`

**Verified:** a JIT runner idles happily after registering ("Listening for Jobs"),
so a warm pool removes container startup and config minting from the dispatch
path entirely.

```console
$ ... --min-warm 2 --max 8
```

Measured on the homelab: with two warm runners listening, a dispatched job reached
`in_progress` in **5 s** — and most of that is GitHub's own queue, since the runner
was already waiting. The pool then refilled to 2 on the next pass after the job
consumed one.

The trade is explicit: idle runners cost memory while idle. `--min-warm 0` keeps
the original scale-from-zero behaviour.

### 4. One image for all labels → `--serve label=image`

```console
$ python3 controller/runletd.py \
    --serve runlet-runner \
    --serve runlet-py=ghcr.io/you/runlet-runner:py \
    --serve runlet-docker=ghcr.io/you/runlet-runner:docker
```

Each label gets its own image and its own pool, with independent `--max`. A job
requesting `runs-on: [runlet-py]` can only be served by a runner minted with that
label, so the mapping is unambiguous.

## What is still not solved

- **~700 ms job fetch** on the critical path inside the agent. Not a controller
  problem; it needs change 3 in the runner (start the worker before the fetch).
- **~3 s queue time at GitHub.** Not addressable at all.
- **No queue-depth signal.** GitHub exposes queued runs, not queue latency, so the
  controller cannot pre-emptively grow beyond `min-warm` before work is visible.
