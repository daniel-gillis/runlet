# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-10

First release. The project is usable but new.

### Added

- **Patch series** against `actions/runner` commit `310a370`, dispatched before the
  first lock renewal and acknowledging jobs concurrently with the job fetch.
- **Runner image** built from the patched agent, published for `linux/amd64` and
  `linux/arm64` with SBOM and provenance attestation.
- **Controller** (`controller/runletd.py`) that scales from zero: it watches queued
  jobs, mints a single-use JIT configuration per job, and starts one container per
  job. Supports multiple repositories, label-to-image mapping, warm pools, and
  optional webhook wakeup.
- **Measurements** for every claim, with the measurement defects recorded alongside
  the results.
- **Compatibility study** showing that 24 of the 25 most-used GitHub Actions declare
  `node20`, which the agent bundles.
- **CI** covering controller tests, agent formatting, linting and tests, patch
  application against a clean upstream checkout, and an image build.

### Performance

- Job assigned to first step: 1731 ms to 1470 ms at the median, and 2460 ms to
  1559 ms at the maximum, measured on a single host over three runs per variant.

### Known limitations

- One job per container. Reuse requires token-based registration and accepting
  state carried between jobs.
- Approximately 700 ms of GitHub job-fetch latency and approximately 3 seconds of
  queue time remain on the critical path and are not addressable here.
- Linux only.
- No per-job toolchain images beyond the label-to-image mapping.
- GitHub Enterprise Server is untested.
