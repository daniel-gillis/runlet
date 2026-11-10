<p align="center">
  <img src="assets/logo.svg" width="88" alt="runlet">
</p>

<h1 align="center">runlet</h1>

<p align="center"><em>Cold-start CI runners in about a second. Scale to zero, stay at zero.</em></p>

<p align="center">
  <img alt="status" src="https://img.shields.io/badge/status-research%20(design%20phase)-orange">
  <img alt="licence" src="https://img.shields.io/badge/licence-MIT-blue">
  <img alt="cold start" src="https://img.shields.io/badge/target%20p99%20cold%20start-under%203s-informational">
</p>

> **Status: design phase.** There is no implementation yet. This repository holds
> a protocol specification, a compatibility study, and the measurements that
> decide whether the idea is worth building. Nothing here is production software.
>
> **Measured, 2026-11-10:** with two idle self-hosted runners, GitHub took
> **3–46 s** to assign a queued job, and the agent spent **3–4 s** executing a
> trivial one. That puts a hard external floor under any end-to-end claim — see
> [`docs/measurements.md`](docs/measurements.md). runlet's own target is therefore
> *assigned → first step in under a second*, not "push to running in 2 s".

---

## The idea

Self-hosted runners are stuck between two bad options:

- **Persistent runners** start jobs instantly but carry state between jobs and
  idle 24/7.
- **Ephemeral runners** (ARC et al.) are clean but take **30–60 seconds** from
  "job queued" to "job running" — scheduling, image pull, registration, checkout.

`runlet` is an attempt at the third option: **ephemeral runners that start in
about a second**, so scale-to-zero costs nothing and still feels instant.

## Why it might be possible

The 30–60 s is not one problem, it is six, and most of them are avoidable:

| Stage | Typical | Target | How |
|---|---|---|---|
| queued → noticed | 2–30 s | *GitHub's, measured 3–46 s* | not ours to optimise |
| image materialised | 5–60 s | 0.1–0.4 s | lazy-pull / snapshot, never a full pull |
| container start | 0.1–0.3 s | 0.05–0.15 s | minimal rootfs or microVM restore |
| registration | 0.3–1.0 s | **0 s** | JIT configs pre-minted in bulk |
| job received | 0.1–0.5 s | 0.1–0.5 s | upstream floor |
| workspace | 1–20 s | 0.1–0.5 s | node-local git mirror + overlay |

Two of those are the ones nobody optimises: minting runner credentials ahead of
demand removes an API round-trip from every cold start, and a node-local git
mirror removes what is usually the single largest line item.

## The compatibility question, answered with data

The obvious objection: *a small, fast runner can't run real actions.*

We measured it. `tools/compat_sweep.py` reads the `runs.using` field from the
`action.yml` of the most-used actions:

**24 of 25 (96%) declare `node20`.** One is a composite action.

That means a runner providing `sh`, `git`, a Node 20 runtime, `tar`, `sha256sum`
and network egress executes the overwhelming majority of the marketplace — and
the `setup-*` actions *download* their toolchains at runtime, so nothing has to be
baked in. Full table: [`docs/compatibility.md`](docs/compatibility.md).

## Design

```
   GitHub ──webhook──▶ [source adapter] ──▶ ledger ──▶ scheduler ──▶ [materialiser] ──▶ runner
                                                                       │
                                                     content-addressed overlays ─────┘
```

- **`runletd`** — the materialiser. A single static Rust binary that turns
  "job needs `{sh, git, node20}`" into a running environment in ~200 ms using
  pre-built, content-addressed layers.
- **Ledger + scheduler** — level-triggered, lease-based; see
  [`PROTOCOL.md`](PROTOCOL.md). Nothing is announced once, so the "job orphaned
  on listener restart" class of bug cannot exist.
- **Bring your own agent** — the GitHub runner agent is MIT and must speak
  GitHub's protocol; we run it, we don't replace it. What we replace is
  everything around it.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the component breakdown and
[`PROTOCOL.md`](PROTOCOL.md) for the wire semantics and invariants.

## What this is not

- **Not a fork of ARC.** Different architecture, different failure model.
- **Not a workflow engine.** Jobs are opaque; runlet moves them, it doesn't
  interpret them.
- **Not a replacement for GitHub's runner protocol.** That is impossible by
  construction; see Protocol §1.
- **Not one giant image.** A single image that runs every toolchain is a 1 GB
  image, and that is the latency we are trying to delete.

## Roadmap

1. **Measure the floor.** Timestamp pod-start vs job-start on a minimal runner to
   find GitHub's own assignment latency. *This decides whether the target is
   reachable.*
2. **Prototype `runc-tiny`** — distroless base + Node 20 + lazy-pull; measure
   20 jobs, p50/p95/p99.
3. **Prove the invariants** — kill the scheduler mid-run and the runner mid-job;
   assert zero orphans and zero double-executions.
4. **Overlays** — `python`, `docker`, `go` profiles; measure marginal cold-start
   cost of each.

If step 1 shows GitHub's floor is above ~2 s, the honest product claim becomes
"p99 under 3 s" rather than "1 s", and the design changes accordingly.

## Licence

MIT. Not affiliated with GitHub, Inc.
