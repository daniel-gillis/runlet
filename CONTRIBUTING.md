# Contributing

## What this project expects

Claims need measurements. If a change is described as faster, smaller or more reliable, include the command and the output. Prose is not evidence.

The patch stays small. It changes two files in `actions/runner`, because it has to be rebased onto every upstream release. A change that touches more needs a reason that survives review.

## Setup

Docker, Python 3.9 or newer, and Rust 1.89 or newer.

```console
$ python3 -m unittest discover -s controller -v
$ cd agent && cargo test && cargo clippy --all-targets -- -D warnings
$ docker build -f runner/Dockerfile -t runlet-runner:dev .
```

## Changing the patch

```console
$ git clone https://github.com/actions/runner.git && cd runner
$ git checkout $(cat ../runlet/patches/UPSTREAM_COMMIT)
$ git apply ../runlet/patches/runlet-fast-path.patch
# edit, then regenerate
$ git diff > ../runlet/patches/runlet-fast-path.patch
```

CI applies the patch to the pinned commit and fails if it touches a file other than the two it is allowed to touch.

## Measuring a change

```console
$ python3 tools/measure_agent.py --variant my-change --runs 3
```

Use at least three runs and report the median and the maximum. Two traps that produced wrong results here:

- Identical spread across runs usually means the instrument is broken, not that the code is deterministic.
- A runner takes the oldest queued job, so a stale queued run will be measured instead of the new one. The harness drains the queue first.

## Pull requests

- Describe what changed, what you measured, and what you did not measure.
- Update the documentation the change affects. `docs/performance.md` holds the numbers.
- Run the full test suite. CI also checks that the patch applies and stays within its two files.
- Write documentation and comments in US English, in a neutral voice, without first person narration.

## Reporting a problem

Include the agent version, the image tag or digest, and the output of one job run with `RUNNER_PERFLOG` enabled. If you are not sure whether the problem exists without the patch, test on an unmodified runner first.
