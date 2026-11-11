# FAQ

## Does this change what workflows can run?

No. The patch changes when the runner starts work. Steps, logs, isolation and `runs-on` behave as before.

## Why is the gain only 15 percent?

Most of a job's startup belongs to GitHub. Measured at half-second resolution, a run takes 1 to 2 seconds for GitHub to create the job from the workflow, and a runner that is already polling picks it up in under a tenth of a second. After that, the job definition takes 700 to 840 ms to fetch, then roughly 470 ms of agent work before the first step runs.

The patch removes the parts that were overhead: a lock renewal that blocked dispatch and an acknowledge that blocked the fetch. The rest is GitHub's latency.

## Why patch the agent instead of writing a new one?

The protocol is GitHub's and has no public specification. Session creation, encrypted message handling, job fetch and log upload live inside a .NET SDK vendored with the runner source. Reimplementing them means reverse engineering several thousand lines with no compatibility guarantee, and GitHub can change any of it without notice.

The agent is MIT licensed and the changes are small. [design.md](design.md) covers the reasoning.

## Does GitHub support patched runners?

Self-hosted runners are yours to build, and the agent is distributed as source. This is still an unsupported modification. If you report behavior that only appears with the patch, expect to be asked to reproduce it on an unmodified runner.

## Can I use this with Actions Runner Controller?

Not directly. ARC starts the unmodified agent image and manages its own lifecycle. You could substitute the image and let ARC handle scaling. The [controller](controller.md) in this repository does that job for simple cases and does not replace ARC's feature set.

## Does it work with GitHub Enterprise Server?

Untested. Everything here was measured against github.com. Enterprise Server may use different endpoints or version requirements.

## Do I have to use JIT configurations?

For the image, yes. It starts from `RUNNER_JIT_CONFIG` and serves one job. The patch itself is independent of configuration and applies to a runner registered with a token.

## Why one job per container?

A JIT-configured runner is single-use because the server sets `Ephemeral` in the configuration it mints. Reusing a runner means token-based registration and accepting state carried between jobs.

## Can I run it without Docker?

Yes. Build the agent with the patch applied and run it the way you run runners now, including under systemd or in a virtual machine. The container is packaging.

## How do I keep up with upstream releases?

The patch is pinned to a commit in `patches/UPSTREAM_COMMIT` and changes two files. A new upstream release means rebasing those two hunks. CI applies the patch to the pinned commit on every push, so a broken patch fails the build.

## What does it cost to run?

The image is about 730 MB. With `--min-warm` above zero, idle runners hold memory while waiting. The default is zero, which means nothing runs until a job is queued.

## Is it production ready?

It builds, runs real jobs, and scales from zero. It is also new, maintained by one person, and has no long-term usage behind it. Read the patch and the numbers before deploying it.
