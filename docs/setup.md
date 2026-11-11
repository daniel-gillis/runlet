# Setup

You can use runlet as a container image or as a patch to a runner build you already have.

The image runs one job and exits. A JIT-configured runner takes exactly one job by GitHub's design, so running a fleet needs the [controller](controller.md), which mints configurations and starts containers as jobs appear.

## Build the image

```console
$ docker buildx build \
    --platform linux/amd64 \
    -t ghcr.io/<you>/runlet-runner:dev \
    ./runner
```

The build clones `actions/runner` at the commit recorded in
`patches/UPSTREAM_COMMIT`, applies the patch, and compiles the agent. It takes five to ten minutes the first time.

The build fails if the patch does not apply, so an image that exists is an image with the patch in it.

## Mint a runner configuration

A JIT configuration removes registration from the startup path. There is no token exchange and no credential written to disk.

```console
$ gh api -X POST repos/<owner>/<repo>/actions/runners/generate-jitconfig \
    -f name=runlet-$(date +%s) \
    -F runner_group_id=1 \
    -f 'labels[]=runlet' \
    -f work_folder=_work \
  | jq -r .encoded_jit_config > /tmp/jit.txt
```

Passing `labels[]=runlet` gives the runner that label and nothing else. `self-hosted` is not added automatically. The workflow has to say `runs-on: [runlet]`.

## Run one job

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    ghcr.io/<you>/runlet-runner:dev
```

The container registers, takes one job, and exits. A workflow that uses it:

```yaml
jobs:
  build:
    runs-on: [runlet]
    steps:
      - uses: actions/checkout@v4
      - run: make test
```

JavaScript actions run because the agent bundles its own Node runtime. Docker actions need the socket mount shown above.

## Confirm the patch is active

The patch adds performance counters. Enable them and run one job:

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -e RUNNER_PERFLOG=/tmp/perf \
    -v /tmp/perf:/tmp/perf \
    ghcr.io/<you>/runlet-runner:dev
$ grep -E "SentJobToWorker|JobRequestRenewed" /tmp/perf/Runner.perf
```

`SentJobToWorker` appears before `JobRequestRenewed` when the patch is active. On an unpatched runner the order is reversed.

## Patch your own build

```console
$ git clone https://github.com/actions/runner.git
$ cd runner && git checkout $(cat ../patches/UPSTREAM_COMMIT)
$ git apply /path/to/patches/runlet-fast-path.patch
$ cd src && ./dev.sh layout Release linux-x64
```

This needs .NET SDK 8.0.427, which `global.json` pins and `dev.sh` downloads.

## What the patch changes

| Change | Effect | Measured |
| --- | --- | --- |
| Dispatch before lock renewal | Worker starts without waiting for an HTTP round trip to GitHub | 1731 to 1470 ms at the median, 2460 to 1559 ms at the maximum |
| Acknowledge concurrent with fetch | The best-effort acknowledge no longer blocks the fetch | 1451 to 1262 ms in the broker flow |
| Perf counters | Shows where time goes on your runner | Not applicable |

[performance.md](performance.md) has the numbers and the measurement defects found while producing them.

## What it does not change

Job execution, logs, isolation and step semantics are unchanged. Queue time at GitHub, about three seconds, stays where it is.

## Requirements

Linux, amd64 or arm64. One job per container. The image is about 730 MB: Debian slim, git and archive tools, and the agent layout, of which Node 24 is about 200 MB.

The image ships no Python, Go or Java. `setup-python` and similar actions download a toolchain at runtime. If a step needs a tool and no action installs it, add it to the image.
