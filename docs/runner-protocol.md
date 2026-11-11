# The runner protocol

Notes on what a GitHub Actions agent must do on the wire, taken from the agent source and from a live configuration. Useful if you are debugging a runner or considering writing your own.

## Why this cannot be redesigned

The workflow definition, the job message, the log format and the result payload are all GitHub's. An agent that does not speak this protocol receives no jobs. The design freedom is in how fast an agent does it, not in what it says.

## Flow

```
1. JIT configuration    single-use, minted by the server
2. Create session       returns a session id and an encryption key
3. Long-poll messages   blocks until a job arrives
4. Decrypt              messages are encrypted with the session key
5. Run the job          fetch the definition, execute steps, stream logs
6. Report               complete the job, upload logs and timeline
7. Acknowledge, delete  remove the message, then exit or continue
```

## JIT configuration

`POST /repos/{owner}/{repo}/actions/runners/generate-jitconfig` returns
`encoded_jit_config`, which is base64 of a JSON object holding three base64 documents:

```
base64(
  { ".runner":                base64(json),
    ".credentials":           base64(json),
    ".credentials_rsaparams": base64(json) })
```

`.runner` holds the agent settings. Fields observed in a live configuration:

```json
{"AgentId":"168","AgentName":"runlet-probe","DisableUpdate":"True",
 "Ephemeral":"True","PoolId":"1","PoolName":null,
 "ServerUrl":"https://pipelines.actions.githubusercontent.com/...",
 "WorkFolder":"_work","GitHubUrl":"https://github.com/...",
 "ServerUrlV2":"https://broker.actions.githubusercontent.com/...",
 "UseV2Flow":"True"}
```

Three details cost a build each to discover:

- Identifiers are strings, not numbers.
- Booleans are capitalized strings, so the document stringifies everything.
- `Ephemeral` is `true` by construction. A JIT runner serves exactly one job, and the client cannot change that.

`.credentials` is an OAuth client, not a bearer token:

```json
{"Scheme":"OAuth","Data":{"ClientId":"...","AuthorizationUrl":"..."}}
```

The agent exchanges the client id for an access token before it talks to the queue. The grant type is inside `GitHub.Services.OAuth`.

`.credentials_rsaparams` is a 2048-bit RSA private key. The server encrypts job messages to it, so an agent that skips it can connect and never read a job.

## Session and messages

`CreateAgentSession(poolId)` returns a session with an `EncryptionKey`. The agent then long-polls:

```
GetAgentMessage(poolId, sessionId, lastMessageId, runnerStatus,
                runnerVersion, os, osArchitecture, disableUpdate)
```

The poll carries the runner's status, version, OS and architecture on every request, which is why the server can decide whether to hand over work.

`AcknowledgeMessage(runnerRequestId)` confirms the job is taken, with a five-second timeout on the agent side. It is documented as best-effort.

## Broker flow

Modern runners use `ServerUrlV2`. The message body is a small `RunnerJobRequestRef`, and the agent then fetches the job definition over HTTP. That fetch measured 697 to 719 ms on the homelab.

The ref carries `ShouldAcknowledge`, `RunnerRequestId`, `RunServiceUrl` and `BillingOwnerId`.

## What is not verified

- The cipher framing for message decryption. The expected shape is RSA-wrapped key plus AES with an IV in the message, but this has not been exercised.
- How much of the log and timeline setup can overlap with step execution. If it can, part of the remaining startup cost goes away.

Both are documented here rather than guessed at in code.
