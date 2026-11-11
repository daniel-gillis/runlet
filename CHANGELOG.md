# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 - 2026-11-10

First release.

### Added

- Patch for `actions/runner` at commit `310a370` (recorded in `patches/UPSTREAM_COMMIT`) that dispatches jobs before waiting for the lock renewal and acknowledges them while fetching the job.
- Container image built from the patched agent, published for amd64 and arm64 with attestations.
- Controller that starts one container per queued job and scales back to zero, with worker pools, label routing, multiple repositories and an optional webhook trigger.
- Measurement harness and the results in `docs/performance.md`.
- Compatibility study of the most-used actions in `docs/compatibility.md`.
- CI covering controller tests, agent formatting and tests, patch application, and an image build.

### Measured

- Assigned to first step: 1731 ms to 1470 ms at the median, 2460 ms to 1559 ms at the maximum, over three runs per variant on one host.

### Known limitations

- One job per container. Reuse would need token registration and state handling between jobs.
- About 700 ms of job fetch and about 3 seconds of queue time remain on the critical path.
- Linux only.
- GitHub Enterprise Server untested.
