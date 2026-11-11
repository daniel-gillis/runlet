# Contributing

## Before you start

This project has one hard rule: **claims require measurements.** If a change is
described as faster, smaller, or more reliable, include the command that shows it
and the output. Prose is not evidence.

The second rule is that the patch must stay small. It modifies two files in
`actions/runner`. A change that touches more needs a strong reason, because the
patch has to be rebased onto every upstream release.

## Development setup

Requirements: Docker, Python 3.9 or newer, and Rust 1.89 or newer for the agent
crate.

```console
$ python3 -m unittest discover -s controller -v      # controller tests
$ cd agent && cargo test && cargo clippy --all-targets -- -D warnings
$ docker build -f runner/Dockerfile -t runlet-runner:dev .
```

To change the patch itself:

```console
$ git clone https://github.com/actions/runner.git && cd runner
$ git checkout $(cat ../runlet/patches/UPSTREAM_COMMIT)
$ git apply ../runlet/patches/runlet-fast-path.patch
# edit, then regenerate:
$ git diff > ../runlet/patches/runlet-fast-path.patch
```

## Measuring a change

The harness runs one real GitHub job against a locally built agent and reports where
the time went.

```console
$ python3 tools/measure_agent.py --variant my-change --runs 3
```

Use at least three runs and report the median and the maximum. Single runs are
noise. Two specific traps, both of which produced wrong results here:

- Identical spread across runs usually means the instrument is broken, not that the
  code is deterministic.
- A runner takes the oldest queued job, so a stale queued run will be measured
  instead of the new one. The harness drains the queue first; keep it that way.

## Pull requests

- Keep the description factual: what changed, what was measured, what was not.
- Update the documentation that the change affects. `docs/performance.md` carries
  every number; `FAQ.md` carries the answers people ask for.
- Run the full test set. CI also verifies that the patch applies to the pinned
  upstream commit and touches only the two files it is allowed to touch.
- New documentation and code comments are written in US English, in a neutral
  voice, without first person narration.

## Reporting a problem

Include the runner version, the image tag or digest, the KUBECONFIG-independent
details of your setup, and the output of one job with `RUNNER_PERFLOG` enabled. If
you are unsure whether the problem also happens without the patch, testing on an
unmodified runner is the fastest way to narrow it down.
