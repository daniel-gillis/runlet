# Design

This page explains why runlet is a patch rather than a new agent, because the first plan was a rewrite and the source did not support it.

## Two protocols, both GitHub's

| Hop | Owner | Replaceable |
| --- | --- | --- |
| Runner to github.com: jobs, logs, artifacts, cache, results | GitHub | No |
| Controller to the broker: sessions, job messages, acquisition | GitHub | No |
| A job exists to a runner executing it | This project | Yes |

A new agent cannot replace the first two. A runner that does not speak the wire format receives no jobs. The design space is the layer that consumes them.

## What the source showed

The protocol is readable. It lives in the runner repository under `src/Sdk/DTWebApi`, `DTPipelines` and `DTLogging`, not behind a package reference. Reading it showed why a port is expensive:

- `TaskAgentHttpClient` builds requests through a route table with `locationId` indirection, so paths are derivable but undocumented.
- Session creation, message envelopes and job requests are typed in `DTPipelines`, which wraps messages before the listener sees them.
- Log streaming, timelines and result uploads form a separate surface.
- The OAuth exchange sits in `GitHub.Services.OAuth`, where the grant type and assertion format are constructed.

Reproducing that in another language means reimplementing several thousand lines of behavior with no specification and no compatibility guarantee. It is weeks of work with a real chance of never reaching parity, and upstream keeps moving.

## What the measurements showed

Instrumenting the existing agent showed that the largest share of job startup was not computation. Two HTTP calls sat on the critical path:

- A lock renewal that blocked dispatch, at 2.4 seconds on a cluster node and 1.2 seconds locally.
- An acknowledge, documented as best-effort, that blocked the job fetch at 133 ms.

Both are ordering decisions inside the agent, so changing them is a small diff in two files.

## The decision

Patch the agent. The changes are narrow, the effect is measurable, and the maintenance surface is two hunks rather than a protocol implementation.

A new agent becomes justified if the project needs something the current one cannot express, such as several jobs per process or a different isolation model. Neither is true today.

## The cost

The patch is rebased onto upstream releases. Because it touches the dispatch path and the broker acknowledgement, and because CI checks that it applies and touches no other files, the rebase stays bounded.
