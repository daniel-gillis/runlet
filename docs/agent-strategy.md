# Reimplement vs. fork: what the source actually says

Status: decision memo. Written after reading the runner's own code, not from
assumptions.

## What is verified

| Fact | Evidence |
|---|---|
| JIT config is double-base64: `.runner`, `.credentials`, `.credentials_rsaparams` | live `generate-jitconfig` response |
| Ids are strings, booleans are `"True"` | same |
| `.credentials` is an OAuth **client**, not a token | same |
| `.credentials_rsaparams` is a 2048-bit RSA key | same |
| The modern flow is the **V2 broker** at `broker.actions.githubusercontent.com` | `BrokerMessageListener.CreateSessionAsync` |
| The wire protocol lives in the vendored VSS SDK | `src/Sdk/DTWebApi`, `DTPipelines`, `DTLogging`, `PipelinesWebApi` |

## What that means

The good news is that the protocol is **readable** — the SDK is in the repository
rather than behind a NuGet blob. The bad news is what readable turns out to be:

- `TaskAgentHttpClient` builds requests through a route table with `locationId`
  indirection, not literal URL strings. The paths are derivable, not documented.
- Session creation, message envelopes and job requests are typed in `DTPipelines`;
  job messages are wrapped by the SDK before they reach the listener.
- Log streaming, timelines and result uploads are a separate surface (`DTLogging`,
  `TaskHttpClient`).
- The OAuth exchange lives inside `GitHub.Services.OAuth` (`VssOAuthCredential`,
  `VssOAuthCredentialFederated`), where the grant type and assertion format are
  constructed — one more thing to reverse-engineer rather than read.

Porting that faithfully to Rust means reproducing several thousand lines of .NET
SDK behaviour with **no specification and no compatibility guarantee**. GitHub can
change any of it; the only thing that would keep working is the official agent.

**Correction to an earlier claim.** I described a from-scratch agent as a
contained piece of work. That was wrong, and the source is what disproves it. The
honest size is weeks, not days, with a meaningful chance of never reaching parity.

## Options

| Option | Cost | Risk | Keeps the 903 ms target achievable? |
|---|---|---|---|
| **A. Narrow fork of the MIT C# agent** | days | low | **yes** |
| B. Full Rust reimplementation | weeks | high — undocumented protocol, breaks on upstream change | yes, eventually |
| C. Optimise around the stock agent | days | none | **no** — the 903 ms is inside the agent |

### Why A is the right first move

The target interval is *inside the agent's own processing*: 903 ms from job
message to first step. Nothing outside the agent can remove it. But the agent is
MIT, and the work is narrow:

1. **Start the first step before the timeline/log service is fully wired.**
   Logging is not on the critical path of step execution; today it appears to be
   initialised first.
2. **Acknowledge the job immediately**, then download and prepare concurrently
   with nothing — the ack is independent of execution.
3. **Skip the update check** (`DisableUpdate` is already true in the JIT config).
3. **Publish native AOT** to remove .NET startup from process start.

Every one of those is a local change to a codebase we already have to build
anyway if we ever want a custom image. And it is *measurable*: the harness that
produced 903 ms is already written and committed.

If the fork gets us to ~400 ms, that is the product claim, proven. If it gets us
to 700 ms, we learn which of the three changes didn't matter — cheaply, and
without a protocol port that might never finish.

### What would justify B later

Only if the fork proves to be a maintenance burden that exceeds the value of the
latency win, or if we need capabilities the agent cannot express (multiple jobs per
process, custom isolation). Neither is true today.
