# Controller

A JIT-configured runner serves one job, so an image on its own cannot serve a queue. `controller/runletd.py` fills that role. It watches for queued jobs, mints a configuration for each, and starts one container per job.

## How a pass works

```
read queued jobs whose labels the profile serves
capacity = max - containers already running
for each queued job, up to capacity:
    mint a single-use configuration with that job's labels
    docker run -d --rm <image> with the configuration in an environment variable
the container takes the job, exits, and is removed by docker
```

## Usage

```console
$ export GITHUB_TOKEN=...            # repository scope is enough
$ python3 controller/runletd.py \
    --repo daniel-gillis/homelab \
    --serve runlet-runner \
    --image ghcr.io/you/runlet-runner:0.1.0 \
    --max 8 \
    --interval 5
```

| Flag | Meaning |
| --- | --- |
| `--repo` | `owner/repo` to serve. Repeat for more repositories. |
| `--image` | Default image for profiles that do not name one |
| `--serve` | A label, or `label=image`. Repeatable. |
| `--max` | Cap on concurrent runners per profile |
| `--min-warm` | Idle registered runners to keep ready |
| `--interval` | Poll period in seconds |
| `--webhook-port` | Listen for `workflow_job` events |
| `--dry-run` | Log decisions, start nothing |
| `--once` | One pass, then exit |

## Label matching

A JIT configuration's label list replaces the defaults. A runner minted with `["runlet-runner"]` has that label and not `self-hosted`. The controller mints each runner with the job's own labels so the match is exact rather than hoped for.

A job with no labels is never served, because a runner started for it could not match.

## State and retries

The controller records jobs and leases in a SQLite ledger, so a restart resumes from the queue. A container that disappears is retried with a higher fencing token, up to `--max-attempts`. [scheduler.md](scheduler.md) describes the ledger, the reconciliation rules and the tests.

A failed mint or a failed `docker run` is logged and the pass continues, so one bad job does not stop the rest.

## Warm runners

`--min-warm N` keeps N registered runners listening before work arrives. A JIT runner idles after registering, so a queued job goes to an agent that is already waiting instead of paying container startup and configuration minting.

With two warm runners on the homelab, a dispatched job reached `in_progress` in 5 seconds, most of which was GitHub's queue. The pool refilled to two on the next pass after the job consumed one.

Idle runners hold memory. `--min-warm 0` restores scale-from-zero.

## Several repositories and images

```console
$ python3 controller/runletd.py \
    --repo owner/a --repo owner/b \
    --serve runlet-runner \
    --serve runlet-py=ghcr.io/you/runlet-runner:py \
    --serve runlet-docker=ghcr.io/you/runlet-runner:docker
```

Each label gets its own image and its own pool with an independent `--max`.

## Webhooks

Polling bounds dispatch latency to `--interval`. A webhook removes that bound:

```console
$ export RUNLETD_WEBHOOK_SECRET=...      # same secret as the GitHub webhook
$ python3 controller/runletd.py --webhook-port 8787 ...
```

The listener verifies `X-Hub-Signature-256` with HMAC-SHA256 and returns 401 for anything else. A `workflow_job` with action `queued` wakes the loop immediately. Polling stays as a fallback, so a missed webhook delays a job by one interval rather than losing it.

## Service unit

```ini
[Unit]
Description=runletd: scale-from-zero controller for runlet runners
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

## Not handled

- One repository set per process. Several instances mean several tokens.
- GitHub exposes queued runs, not queue latency, so the controller cannot grow capacity before a job is visible.
- No per-job toolchain images beyond the label-to-image map.
- The job fetch inside the agent, about 700 ms, is not something the controller can move.
