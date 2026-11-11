# Frequently asked questions

## Does this change what my workflows can run?

No. The patch changes when the runner starts work, not what it executes. Same
agent, same steps, same logs, same isolation, same `runs-on` semantics.

## Why is the improvement only 15 percent?

Because most of a job's startup is not ours to change. Measured on a single host, a
trivial job spends roughly:

- 3 seconds queued at GitHub before any runner is told about it
- 700 ms fetching the job definition after the runner is told
- 200 to 400 ms starting the agent's worker process
- the rest in steps

The patch removes the parts that were pure overhead: a lock renewal that blocked
dispatch and an acknowledge that blocked the fetch. What remains is predominantly
GitHub's own latency.

## Why patch the agent instead of writing a new one?

The protocol is GitHub's and is not documented as a specification. The parts a new
agent would need, including session creation, encrypted message handling, job fetch
and log upload, are implemented inside a vendored .NET SDK that ships with the
runner source. Reimplementing them means reverse engineering several thousand lines
with no compatibility guarantee, and GitHub can change any of it without notice.

The agent is MIT licensed and the changes are small. See
[design.md](design.md) for the full reasoning.

## Does GitHub support patched runners?

Self-hosted runners are yours to build. The agent is MIT licensed and explicitly
distributed as source. That said, this is an unsupported modification: if you file
an issue with GitHub about behavior that only appears with this patch, they will
reasonably ask you to reproduce it on an unmodified runner.

## Can I use this with Actions Runner Controller?

Not directly. ARC manages its own runner lifecycle and starts the unmodified agent
image. You would need to substitute the image and let ARC handle scaling. The
controller in this repository is an alternative for that role, not a replacement for
ARC's full feature set.

## Does it work with GitHub Enterprise Server?

Unknown. Everything here was tested against github.com. Enterprise Server may use
different endpoints or version requirements.

## Do I have to use JIT configurations?

For the container image, yes: it starts from `RUNNER_JIT_CONFIG` and serves one job.
For the patch itself, no. The two changes are to the dispatch path and apply to any
configuration, including a runner registered with a token.

## Why one job per container?

A JIT-configured runner is single-use by GitHub's design. The `Ephemeral` flag is
set in the configuration the server mints, not by the client. Reusing a runner would
require token-based registration and accepting state carried between jobs.

## Can I use it without Docker?

Yes. Build the agent with the patch applied and run it however you already run
runners, including systemd or a virtual machine. The container is a convenience.

## How do I keep up with upstream releases?

The patch is pinned to a specific commit in `patches/UPSTREAM_COMMIT` and applies to
the two files that changed. A new upstream release means rebasing those hunks. CI
applies the patch to the pinned commit on every push, so a broken patch fails the
build rather than shipping.

## What does it cost to run?

The image is approximately 730 MB. With `--min-warm` set above zero, idle runners
hold memory while waiting for work. The default is zero, which means nothing runs
until a job is queued.

## Is it production ready?

It is verified to build, run real jobs, and scale from zero. It is also new, with a
single maintainer and no long-term usage. Treat it as a tool for people comfortable
reading the patch and the measurements before deploying it.
