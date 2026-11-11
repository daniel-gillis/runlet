# Performance

Every figure here comes from a real GitHub job executed by a locally built agent. The harness is `tools/measure_agent.py`.

## Results

| Metric | Baseline | Patched | Change |
| --- | --- | --- | --- |
| Assigned to first step, median | 1731 ms | 1470 ms | -15% |
| Assigned to first step, maximum | 2460 ms | 1559 ms | -37% |
| Broker flow, acknowledge phase | 133 ms | 7.7 ms | -94% |
| Broker flow, total | 1451 ms | 1262 ms | -13% |

The maximum matters as much as the median. The lock renewal is a network call with variable latency, and it sat on the critical path, so every job inherited the tail of GitHub's response time.

## Where a trivial job spends its time

The agent's own counters (`RUNNER_PERFLOG`) timestamp each stage:

```
job message received
  lock renewal, HTTP to GitHub       2.4 s on a cluster node, 1.2 s locally
  spawn Runner.Worker               120 to 300 ms of .NET startup
  acknowledge, HTTP                 133 ms
  fetch job definition, HTTP        697 to 719 ms
  first step executes
```

The patch removes the renewal and the acknowledge from that path. The fetch stays.

## Measurements

### Assigned to first step

Same host, same job, three runs each.

| Variant | Runs | Median | Spread | Maximum |
| --- | --- | --- | --- | --- |
| Baseline | 3 | 1731 ms | 1529 to 2460 ms | 2460 ms |
| Patched | 3 | 1470 ms | 1442 to 1559 ms | 1559 ms |

### Broker flow phases

| Phase | Before | After |
| --- | --- | --- |
| Message received to reference parsed | 1 ms | 3 ms |
| Reference parsed to acknowledge complete | 133 ms | 7.7 ms |
| Acknowledge to job fetched | 719 ms | 697 ms |
| Job fetched to worker started | 10 ms | 10 ms |
| Total | 1451 ms | 1262 ms |

Parsing the job reference takes about a millisecond. The rest is network: 852 ms of the original 1451 ms, or 59 percent.

### Host comparison

A runner on a cluster node measured 3.66 seconds from job message to first output. The same code on an Apple silicon workstation measured 1.78 seconds. The difference is CPU speed, which is why comparisons in this document stay on one host.

## Measurement corrections

Three defects in the harness produced plausible but wrong results during this work. Anyone reproducing these numbers should check the same things.

**Stale performance files.** The parser keeps the first occurrence of each phase, and the directory was not cleared between runs, so three "independent" runs reported identical timestamps. Identical spread across runs means the instrument is broken. The harness now clears the directory before each run.

**Wrong host.** The harness read the first runner pod's log, but with two runners a job can land on either one, so the step timestamp was compared against another pod's log line. It now reads the hostname from the job output and queries that pod.

**Leftover queued runs.** An ephemeral runner takes the oldest queued job, so runs abandoned by an interrupted measurement were served instead of the new one, and the runner then exited, leaving the fresh run queued. The harness now drains the queue before starting an agent. Draining afterward races with the agent, which can pick up a job that is being cancelled.

One correction affected conclusions rather than measurements. The first interval figure was anchored on the `Running job:` line, which the agent prints after the lock renewal. It read 903 ms while the true interval was 3.66 seconds. The performance counters showed the difference.

## Method

```console
$ python3 tools/measure_agent.py --variant patched --runs 3
```

The harness drains queued runs for the target workflow, mints a single-use configuration with an exclusive label, starts an agent, waits for it to report online, dispatches one job to that label, and waits for completion. It then correlates the agent's `MessageReceived` counter with `RUNLET_STEP_EPOCH` printed by the first step, terminates the agent, and deletes the runner registration.

Both timestamps come from the same process clock, so the difference contains no skew. Every wait is bounded and fails with a reason rather than hanging.
