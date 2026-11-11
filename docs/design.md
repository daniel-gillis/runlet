# Why a patch series instead of a new agent

This document records why the project takes the form it does. It exists because the
first design proposed a new agent written from scratch, and the source did not
support that plan.

## What the runner protocol actually is

There are two protocols in a self-hosted GitHub Actions setup, and GitHub owns both.

| Hop | Owner | Replaceable |
|---|---|---|
| Runner to github.com (jobs, logs, artifacts, cache, results) | GitHub | No |
| Controller to the broker (sessions, job messages, acquisition) | GitHub | No |
| "A job exists" to "a runner is executing it" | This project | Yes |

A new agent cannot replace the first two: a runner that does not speak the wire
format receives no jobs. The design space is the layer that consumes them.

## What the source showed

The protocol is readable. The relevant implementation is vendored in the runner
repository under `src/Sdk/DTWebApi`, `DTPipelines` and `DTLogging` rather than
hidden behind a package reference. What that readability revealed:

- `TaskAgentHttpClient` constructs requests through a route table with
  `locationId` indirection. The paths are derivable but not documented.
- Session creation, message envelopes and job requests are typed in `DTPipelines`,
  which wraps messages before the listener sees them.
- Log streaming, timelines and result uploads are a separate surface.
- The OAuth exchange lives in `GitHub.Services.OAuth`, where the grant type and
  assertion format are constructed.

Reproducing that faithfully in another language means reimplementing several
thousand lines of behavior with no specification and no compatibility guarantee.
The size is weeks of work, not days, with a real chance of never reaching parity,
and the maintenance burden never ends because upstream keeps moving.

## What the measurements showed

Instrumenting the existing agent revealed that the largest single component of job
startup was not computation. It was two HTTP round trips placed on the critical
path:

- A lock renewal that blocked dispatch, measured at 2.4 seconds on a cluster node.
- An acknowledge, documented as best-effort, that blocked the job fetch at 133 ms.

Both are ordering decisions in the agent, not architectural commitments. Changing
them is a small, reviewable diff in two files.

## The decision

Patch the agent. The changes are narrow, the effect is measurable, and the
maintenance surface is two hunks rather than a protocol implementation.

If the project later needs something the agent structurally cannot express, such as
multiple jobs per process or a different isolation model, a new agent becomes
justified. Neither is the case today.

## What this costs

The patch must be rebased onto upstream releases. Because it touches only the
dispatch path and the broker acknowledgement, and because CI verifies that it
applies and that it touches no other files, the rebase is bounded work rather than a
fork to maintain.
