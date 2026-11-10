# Agent protocol — what a from-scratch agent must speak

Derived from `actions/runner` (MIT), specifically
`src/Runner.Listener/MessageListener.cs`. This note records only what the source
actually shows; anything marked **unverified** has not been exercised yet.

## Why we cannot design this ourselves

The workflow definition, the job message, the log format and the result payload
are all GitHub's. An agent that does not speak this protocol receives no jobs. The
design freedom is entirely in *how fast we do it*, not in what we say.

## Flow

```
1. JIT configuration     runner starts with a pre-minted, single-use config
2. Create session        ↳ session id + encryption key
3. Long-poll messages    ↳ blocks until a job arrives
4. Decrypt               ↳ messages are AES-encrypted with the session key
5. Run the job           ↳ download job definition, execute steps, stream logs
6. Report                ↳ complete the job, upload logs/timeline
7. Acknowledge / delete  ↳ remove the message, renew or exit
```

### 1. JIT configuration

The runner is started with `--jitconfig <base64>`. Decoding yields a JSON document
carrying at least the agent identity, pool id, server URL and work folder. The
credential inside is **single-use**: a runner started from a JIT config can serve
exactly one job, which is precisely the ephemeral semantics runlet wants.

**Unverified:** the exact field names and whether the token is a bearer token or
an OAuth client credential in the JIT document. Confirm against
`src/Runner.Listener/Configuration/ConfigurationManager.cs` before implementing.

### 2. Create session

`CreateAgentSessionAsync(poolId, …)` returns a session with `SessionId` and an
`EncryptionKey`. The same call carries the runner's identity and capabilities.
Failures are retried with a backoff (`_sessionCreationRetryInterval`).

### 3. Long-poll

```
GetAgentMessageAsync(poolId, sessionId, lastMessageId, runnerStatus,
                     runnerVersion, os, osArchitecture, disableUpdate)
```

Notable: the poll carries the **runner's status, version, OS and architecture**
on every request. That is why a second of latency appears even with an idle
runner — the server decides whether to hand over work based on that payload.

### 4. Decryption

`DecryptMessage` uses the session's `EncryptionKey`. **Unverified:** the cipher and
the framing of the IV (separate field vs prefixed to the body).

### 5–6. Execution and reporting

The job message references a job definition that must be fetched, then each step
is executed and its output streamed to the log service, with a timeline of step
records. **Unverified:** how much of this can be deferred until after the first
step starts. This is the single biggest opportunity: if log/timeline setup can run
*concurrently* with step execution rather than before it, it comes off the 903 ms.

### 7. Acknowledgement

`AcknowledgeMessageAsync(runnerRequestId, sessionId, …)` with a **5-second**
timeout, and `DeleteAgentMessageAsync` to remove it. Acknowledge is what tells
GitHub the job has been taken; it should fire as soon as the job is accepted, not
after it completes.

## Where the 903 ms probably goes

Ranked by expected payoff, to be confirmed by instrumentation rather than assumed:

1. **Serial setup before the first step** — session/job-download/log-service
   initialisation. Candidate for concurrency with step start.
2. **Job definition download** — at least one network round trip to GitHub;
   unavoidable but measurable.
3. **Step preparation** — writing the step script, resolving the shell, building
   the environment.

## Non-negotiables

- **Do not reimplement the workflow semantics.** Steps are opaque; we execute
  them and report.
- **Acknowledge early, execute late.** Anything after acknowledgment is off the
  measured interval.
- **Never hold a lease on a job we have not started.** Ephemeral, single-use
  runners make retry safe.
