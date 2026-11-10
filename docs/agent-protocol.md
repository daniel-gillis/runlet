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

### 1. JIT configuration — VERIFIED against a live config

`POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig` returns
`encoded_jit_config`, which is **double-base64**:

```
base64(                                        <- the --jitconfig argument
  { ".runner":                base64(json),
    ".credentials":           base64(json),
    ".credentials_rsaparams": base64(json) })
```

`.runner` (real field names and types):

```json
{"AgentId":"168","AgentName":"runlet-probe","DisableUpdate":"True",
 "Ephemeral":"True","PoolId":"1","PoolName":null,
 "ServerUrl":"https://pipelines.actions.githubusercontent.com/…",
 "WorkFolder":"_work","GitHubUrl":"https://github.com/…",
 "ServerUrlV2":"https://broker.actions.githubusercontent.com/…",
 "UseV2Flow":"True"}
```

Three traps, all of which cost a build to discover:

- **Ids are strings** (`"AgentId":"168"`), not numbers.
- **Booleans are capitalised strings** (`"True"`) — the document stringifies
  everything, so `serde` needs lenient handling for both.
- **`Ephemeral` is `true` by construction**: a JIT runner serves exactly one job.
  That is the semantics runlet wants, and it is not configurable.

`.credentials` is an **OAuth client**, not a bearer token:

```json
{"Scheme":"OAuth","Data":{"ClientId":"<36 chars>","AuthorizationUrl":"<url>"}}
```

So the agent must exchange `ClientId` + `AuthorizationUrl` for an access token
before it can talk to the queue. **Unverified:** the grant type and whether the
request is form-encoded client-credentials.

`.credentials_rsaparams` is a 2048-bit RSA private key (`modulus`, `exponent`,
`d`, `p`, `q`, `dp`, `dq`, `inverseQ`). It is what the server encrypts job
messages *to*, so an agent that ignores it can connect and never read a job.

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

`DecryptMessage` uses the session's `EncryptionKey`, and the RSA private key from
the JIT document is what makes that key readable. **Still unverified:** the hybrid
scheme (expected: RSA-OAEP unwrapping a symmetric key, then AES-CBC with an IV
carried in the message) and the field names on the wire. This is the next thing to
read from `RunnerService.cs`/`TaskAgentSession`, because guessing a cipher framing
produces an implementation that looks correct and silently fails.

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

## Status

Implemented and tested against a live config (8 unit tests):

- JIT decoding, including the double-base64 envelope, string ids, string booleans
  and the OAuth client structure.
- Endpoint selection that honours `UseV2Flow` — live value resolves to
  `https://broker.actions.githubusercontent.com/`.

Not yet implemented: token exchange, session creation, message poll, decryption,
job execution, reporting.

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
