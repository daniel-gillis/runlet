# Architecture

Three components, one rule: **nothing on the critical path may talk to a network
it does not control.**

## 1. The materialiser (`runletd`)

A single static Rust binary. Given a job's needs, it produces a running
environment. Everything it does is a local operation on pre-built, content-addressed
data.

```
needs: {sh, git, node20}     ──▶  mount erofs layers (read-only, verified)
                             ──▶  bind node-local git mirror
                             ──▶  create writable overlay upper (tmpfs)
                             ──▶  exec the agent                    ~200 ms
```

Design rules:

- **No image pull on the hot path.** Layers are fetched by a background
  prefetcher ahead of demand and stored content-addressed. `runletd` mounts them;
  it never downloads them.
- **EROFS + fs-verity**, not FUSE. FUSE costs a context switch per page fault and
  makes a "0.2 s" claim impossible. Direct block access via loopback or
  `/dev/erofs` is the point.
- **Overlay upper is tmpfs.** The base is immutable and shared; the writable
  layer is memory, so it costs nothing to discard.
- **Dedup by chunk**, not by layer. Bumping Python minor-version should
  materialise the delta, not the interpreter.

### Profiles

Profiles are the "one size fits most" mechanism. A job's labels select a set:

| Profile | Adds | Approx size |
|---|---|---|
| `base` | sh, git, node20, GitHub action shims, tar, sha256 | ~120 MB |
| `python` | CPython + pip wheel cache | +100 MB |
| `docker` | rootless BuildKit, registry cache pointer | +130 MB |
| `go` | Go toolchain | +90 MB |

Overlays compose: `[base, python, docker]` is one job's environment. The base is
never specialised, so its cold-start cost is paid once per node.

## 2. Ledger + scheduler

Level-triggered, lease-based. Full semantics in [`PROTOCOL.md`](PROTOCOL.md).

Key property: the scheduler holds no unique state. It can die and be replaced
mid-queue without losing a job, because the queue is a query over the ledger, not
a stream of messages a listener might miss.

## 3. Source adapters

One per upstream. The GitHub adapter watches `workflow_job` webhooks and writes
`JobOffered` facts; it also reconciles periodically against the REST API in case
a webhook is lost. Adapters are stateless and restartable.

## Latency budget

| Stage | Budget | Owner |
|---|---|---|
| queued → adapter | 50–500 ms | adapter (webhook) |
| adapter → ledger | ≤ 5 ms | local write |
| ledger → lease | ≤ 50 ms | runner already long-polling |
| materialise | 100–400 ms | `runletd` |
| start | 50–150 ms | runc or microVM |
| register + receive | 0 + 100–500 ms | **GitHub** |
| workspace | 100–500 ms | git mirror |

Sum of the parts we own: **≈ 0.3–1.1 s**. The rest is GitHub's and is measurable,
not optimizable.

## Executor profiles

| Profile | Mechanism | Cold start | Isolation |
|---|---|---|---|
| `runc-tiny` | runc + erofs overlays | 0.5–1.5 s | container (namespaces, no privilege) |
| `microvm` | Firecracker + snapshot restore | 0.2–0.6 s | hardware-virtualised |
| `criu` | checkpoint/restore | 0.1–0.3 s | container (restored), fragile, not recommended |

Start with `runc-tiny`: it runs on stock Kubernetes, needs no KVM, no privileged
pods, and no daemon.

## Non-goals

- Interpreting workflows. runlet moves jobs; the agent runs them.
- Provisioning nodes. We scale leases; the cluster scales machines.
- Replacing the GitHub runner agent. It is MIT and must speak GitHub's protocol.
- Being a general-purpose job system. Optimizing for one thing is the only way
  to be fast at it.

## Open questions

1. **What is GitHub's assignment floor?** Measured in roadmap step 1. If it is
   >2 s, the product claim changes.
2. **Is the ledger worth its complexity** versus an in-memory queue plus
   reconciliation? Answerable by fault injection in step 3.
3. **Do overlays actually compose cheaply**, or does each add a mount that costs
   more than the bytes it saves? Measurable in step 4.
4. **Does anyone care enough to self-host it?** The honest risk. ARC is official,
   and "faster" has to be worth a migration.
