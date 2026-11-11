# Performance

Every number here comes from a real GitHub job executed against a locally built
agent. The harness is in [`tools/measure_agent.py`](../tools/measure_agent.py), and
the method is described at the end of this document so the results can be
reproduced or disputed.

## Summary

| Metric | Baseline | With the patch | Change |
|---|---|---|---|
| Job assigned to first step (p50) | 1731 ms | 1470 ms | -15% |
| Same, maximum | 2460 ms | 1559 ms | -37% |
| Broker flow, acknowledge phase | 133 ms | 7.7 ms | -94% |
| Broker flow, total | 1451 ms | 1262 ms | -13% |

The median improvement is modest. The reduction in worst-case latency is the more
significant result: the lock renewal is a network call with unpredictable latency,
and it previously sat directly on the critical path, so every job inherited the
tail of GitHub's response time distribution.

## Where the time goes

A job that runs a single `echo` traverses these stages. The agent's own perf
counters (`RUNNER_PERFLOG`) timestamp each one:

```
job message received
  lock renewal (HTTP to GitHub)       2.4 s on a cluster node, 1.2 s locally
  spawn Runner.Worker (new process)   ~120-300 ms (.NET startup)
  acknowledge (HTTP)                  133 ms
  fetch job definition (HTTP)         697-719 ms
  first step executes
```

Two of those waits are overhead that the patch removes. The last one is GitHub's
own latency and is not addressable here.

## Detailed measurements

### Assign to first step, unpatched and patched

Same host, same job, same harness, three runs each.

| Variant | Runs | p50 | Spread | Maximum |
|---|---|---|---|---|
| Baseline | 3 | 1731 ms | 1529-2460 ms | 2460 ms |
| Patch applied | 3 | 1470 ms | 1442-1559 ms | 1559 ms |

### Broker flow phases

Instrumented with counters around the acknowledge and the job fetch.

| Phase | Before | After change 2 |
|---|---|---|
| Message received to reference parsed | 1 ms | 3 ms |
| Reference parsed to acknowledge complete | 133 ms | 7.7 ms |
| Acknowledge to job fetched | 719 ms | 697 ms |
| Job fetched to worker started | 10 ms | 10 ms |
| Total | 1451 ms | 1262 ms |

Parsing the job reference takes approximately 1 ms. The cost is entirely network
round trips: 852 ms of the original 1451 ms, or 59 percent.

### Cluster comparison

For reference, a runner on a cluster node measured 3.66 s from job message to first
console output, versus 1.78 s on an Apple silicon workstation. The gap is CPU
speed, not configuration, which is why controlled comparisons in this document use
a single host.

## Measurement corrections

Three defects were found in the harness during this work. They are recorded here
because each produced a plausible but wrong result, and because anyone reproducing
these numbers should verify the same things.

**Stale perf files.** The parser keeps the first occurrence of each phase, and the
perf directory was not cleared between runs, so three "independent" runs reported
identical timestamps to the millisecond. Identical spread across runs indicates a
broken instrument. The directory is now cleared before every run.

**Wrong host.** The harness read the log of the first runner pod, but with two
runners a job can land on either one, so the step timestamp was compared against a
different pod's log line. It now reads the hostname from the job output and queries
only that pod.

**Queued runs from earlier sessions.** An ephemeral runner takes the oldest queued
job, so runs left behind by interrupted measurements were served instead of the
new one, and the runner then exited, leaving the fresh run queued indefinitely.
The queue is now drained before the agent starts listening. Draining afterward
races with the agent, which can take a job being cancelled.

A fourth issue affected conclusions rather than measurements: the first
interval figure was anchored on the `Running job:` log line, which the agent prints
after the lock renewal. It measured 903 ms, but the true interval was 3.66 s. The
perf counters corrected this.

## Method

```console
$ export KUBECONFIG=...                       # only for showing a runner pod
$ python3 tools/measure_agent.py --variant patched --runs 3
```

The harness:

1. Drains queued runs for the target workflow.
2. Mints a single-use JIT configuration with an exclusive label.
3. Starts an agent from it and waits until the runner reports online.
4. Dispatches one job to that label and waits for completion.
5. Correlates two timestamps from the same pod clock: the agent's own perf counter
   for `MessageReceived`, and `RUNLET_STEP_EPOCH` printed by the first step.
6. Terminates the agent and deletes the runner registration.

Every wait is bounded and fails with a reason, so a stuck harness reports an error
instead of appearing to be slow code.
