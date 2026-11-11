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

## Post-patch breakdown

One job, after both changes, measured the same way. Total 1264 ms from job message to first console output.

| Stage | Time | What it is | Who owns it |
| --- | --- | --- | --- |
| Reference parsed | 1.5 ms | Decode the broker job reference | Agent |
| Acknowledge | 4.2 ms | Acknowledged concurrently with the fetch | Agent |
| Job definition fetched | 710 ms | HTTP round trip to GitHub | GitHub |
| Worker process started | 12 ms | Spawn `Runner.Worker` | Agent |
| Job sent to worker | 38 ms | Serialize the job, write to the pipe | Agent |
| Lock renewed | 99 ms | Runs concurrently; not on the path | GitHub |
| Worker process up | 18 ms | .NET runtime start (ReadyToRun is already on) | Agent |
| Job message received | 43 ms | Deserialize the job in the worker | Agent |
| Log service started | 53 ms | Job timeline and log queue setup | Agent |
| Job initialized | 83 ms | Parse steps, build contexts | Agent |
| First console output | 202 ms | Prepare the step, spawn the shell, upload the first line | Agent |

Everything we control after the patch totals 554 ms. The 710 ms fetch belongs to GitHub and cannot be addressed from the runner.

## What each remaining stage would cost to remove

| Stage | Saving | Cost | Verdict |
| --- | --- | --- | --- |
| Start the worker before the fetch | up to 70 ms | Refactor process lifecycle across the fetch in `JobDispatcher` | Poor trade: the worker starts in 18 ms, so there is little to hide |
| Run the job in the listener process | about 110 ms | Remove the worker boundary, including its crash isolation and its cancellation model | Weeks of work and the largest compatibility risk in the project |
| Lazy log service setup | up to 53 ms | Defer timeline creation until the first line is emitted | Possible, but the timeline is needed to attach the line at all |
| Skip the listener's parse of the job | about 40 ms | Pass the raw payload through instead of parse, re-serialize, re-parse | Narrow, and it removes validation the listener relies on |
| Reduce first-step preparation | unclear | Measure what dominates the 202 ms: shell spawn, script write, or the first-line upload | Next experiment |

One claim was tested and rejected, and one was first rejected and then confirmed with more samples:

- **Faster log dequeue, first attempt.** Two samples showed 181 ms against 202 ms, which looked like noise, so the change was reverted. That was the wrong call. With five samples on each side the distribution is bimodal without the change and tight with it.
- **ReadyToRun.** Already enabled upstream (`PublishReadyToRunComposite=true`), so the 18 ms worker start is already close to the .NET floor. Trimming would break the reflection this codebase depends on.

## The ceiling

With the patch, the interval is roughly 90 percent GitHub latency and 10 percent our own work. Removing the entire worker boundary, the largest remaining item, would save about 110 ms of 1264 ms, or nine percent, in exchange for owning the process model that gives the agent its crash isolation.

The next experiment worth running is instrumenting the 202 ms first-step stage, because it is the only remaining item where the cause is unknown rather than merely expensive.

## The log dequeue timer

Console lines were uploaded on a fixed timer: 250 ms per batch for the first 240 batches, then 500 ms. A line queued just after a batch waits almost the full interval.

A queue signal was added so the loop wakes when a line arrives and falls back to the timer when the queue is empty. Five runs each, measuring the interval from script prepared to the first line uploaded:

| Runs | Without the signal | With the signal |
| --- | --- | --- |
| 1 | 113 ms | 119 ms |
| 2 | 172 ms | 87 ms |
| 3 | 1031 ms | 83 ms |
| 4 | 1039 ms | 96 ms |
| 5 | | 86 ms |

Without the change, two of four runs took about a second, because the first line happened to land just after a batch. With it, all five landed between 83 and 119 ms.

The median barely moves. The tail is what changes: a second of "nothing appears to be happening" on a job that has already started, which is the interval a user notices most.
