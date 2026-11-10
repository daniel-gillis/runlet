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
