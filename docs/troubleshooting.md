# Troubleshooting

## The job stays queued and nothing starts

**Check the labels first.** A JIT configuration's label list replaces the defaults.
A runner minted with `labels[]=runlet-runner` has that label and not `self-hosted`.
If your workflow says `runs-on: [self-hosted, runlet-runner]`, no runner will ever
match it.

```console
$ gh api repos/OWNER/REPO/actions/runners --jq '.runners[] | "\(.name) \(.status) \([.labels[].name]|join(","))"'
```

Use `runs-on: [runlet-runner]`, or mint with every label the job requires.

## The controller reports capacity but starts nothing

Check that the image exists locally or can be pulled:

```console
$ docker image inspect ghcr.io/daniel-gillis/runlet-runner:0.1.0 >/dev/null && echo present
```

A failed `docker run` is logged and the loop continues, so one bad configuration
does not stop the rest. Look for lines beginning with `!`.

## The container exits immediately and the job never runs

Run it in the foreground to see the agent's own output:

```console
$ docker run --rm -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" ghcr.io/daniel-gillis/runlet-runner:0.1.0
```

The usual causes are an expired configuration (they are short-lived), a
configuration already consumed by another container, or a runner group id that does
not exist in your repository.

## I cannot tell whether the patch is active

Enable perf counters and compare the order of two phases.

```console
$ docker run --rm \
    -e RUNNER_JIT_CONFIG="$(cat /tmp/jit.txt)" \
    -e RUNNER_PERFLOG=/tmp/perf \
    -v /tmp/perf:/tmp/perf \
    ghcr.io/daniel-gillis/runlet-runner:0.1.0
$ grep -E "SentJobToWorker|JobRequestRenewed" /tmp/perf/Runner.perf
```

With the patch, `SentJobToWorker` appears before `JobRequestRenewed`. On an
unpatched runner the order is reversed.

## The image build fails at the patch step

The patch is pinned to a specific upstream commit. Confirm the commit and apply it
by hand:

```console
$ cat patches/UPSTREAM_COMMIT
$ git clone https://github.com/actions/runner.git && cd runner
$ git checkout <that commit>
$ git apply --check /path/to/patches/runlet-fast-path.patch
```

If `--check` fails, the patch was generated against a different revision. Rebase it
rather than working around it, because an image that built with a partial patch
would still advertise the performance of a full one.

## Jobs are slower than the published numbers

Compare like with like. The published figures are from a single host running the
same trivial job; a cluster node measured roughly twice the wall time for the same
patch because of CPU speed. Also account for repository checkout, which the
measurements exclude by using a job that runs one command.

## A runner registration is left behind

Interrupted measurements and crashed containers can leave offline registrations.
They expire, but they can be removed directly:

```console
$ gh api repos/OWNER/REPO/actions/runners \
    --jq '.runners[] | select(.status=="offline") | .id' \
  | xargs -n1 -I{} gh api -X DELETE repos/OWNER/REPO/actions/runners/{}
```

The controller does this for its own runners before each pass.
