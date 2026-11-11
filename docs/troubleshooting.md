# Troubleshooting

## A job stays queued and nothing starts

Check the labels first. A JIT configuration's label list replaces the defaults. A runner minted with `labels[]=runlet-runner` has that label and not `self-hosted`, so `runs-on: [self-hosted, runlet-runner]` matches nothing.

```console
$ gh api repos/OWNER/REPO/actions/runners \
    --jq '.runners[] | "\(.name) \(.status) \([.labels[].name]|join(","))"'
```

Use `runs-on: [runlet-runner]`, or mint the runner with every label the job requires.

## The controller reports capacity but starts no containers

Confirm the image is present or pullable:

```console
$ docker image inspect ghcr.io/daniel-gillis/runlet-runner:0.1.0 >/dev/null && echo present
```

A failed `docker run` is logged and the pass continues, so look for lines starting with `!`.

## The container exits immediately and no job runs

Run it in the foreground to see the agent's output:

```console
$ docker run --rm -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    ghcr.io/daniel-gillis/runlet-runner:0.1.0
```

Common causes are an expired configuration, a configuration already used by another container, and a runner group id that does not exist in the repository.

## I cannot tell whether the patch is active

Enable the performance counters and compare the order of two phases:

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -e RUNNER_PERFLOG=/tmp/perf \
    -v /tmp/perf:/tmp/perf \
    ghcr.io/daniel-gillis/runlet-runner:0.1.0
$ grep -E "SentJobToWorker|JobRequestRenewed" /tmp/perf/Runner.perf
```

With the patch, `SentJobToWorker` comes first. On an unpatched runner the order is reversed.

## The image build fails at the patch step

The patch is pinned to one upstream commit. Apply it by hand to see the conflict:

```console
$ cat patches/UPSTREAM_COMMIT
$ git clone https://github.com/actions/runner.git && cd runner
$ git checkout <that commit>
$ git apply --check /path/to/patches/runlet-fast-path.patch
```

If `--check` fails, the patch was generated against a different revision. Rebase it. Building with a partial patch would produce an image that runs but does not have the performance it claims.

## Jobs are slower than the published numbers

Compare like with like. The published figures come from one host running one trivial job. A cluster node measured about twice the wall time for the same patch because of CPU speed. Repository checkout is also excluded from the measurements, which use a job that runs a single command.

## A runner registration is left behind

Interrupted measurements and crashed containers can leave offline registrations. They expire on their own, or remove them directly:

```console
$ gh api repos/OWNER/REPO/actions/runners \
    --jq '.runners[] | select(.status=="offline") | .id' \
  | xargs -n1 -I{} gh api -X DELETE repos/OWNER/REPO/actions/runners/{}
```

The controller removes its own stale registrations before each pass.
