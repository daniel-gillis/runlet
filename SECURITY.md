# Security policy

## Supported versions

Only the latest release receives fixes. The project ships a container image and a
patch series; upgrading means pulling a new image or rebasing the patch.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/daniel-gillis/runlet/security/advisories/new).
Do not open a public issue for anything exploitable.

Expect an acknowledgement within 72 hours. Fixes ship as a patch release with an
advisory describing the impact and the affected versions.

## Design commitments

These are properties of the code. A way to break one of them is a security bug.

1. **No long-lived credentials.** The image starts from a single-use JIT
   configuration passed in an environment variable. Nothing is written to disk, and
   the credential is invalid after one job.
2. **The controller holds one token.** It is read from `GITHUB_TOKEN` or
   `GH_TOKEN`, is never written to disk, and needs only repository scope.
3. **Webhook requests are verified.** The optional webhook listener validates
   `X-Hub-Signature-256` with HMAC-SHA256 and returns 401 for anything else.
4. **The patch is bounded.** CI fails if the patch touches any file other than
   `src/Runner.Listener/JobDispatcher.cs` and `src/Runner.Listener/Runner.cs`.
5. **The agent runs unmodified apart from the patch.** The image is built from a
   pinned upstream commit, and the patch is applied with `git apply`, which fails
   the build if it does not apply cleanly.

## Threat model

The components that matter:

- **A compromised runner container** can read anything the job can read. It runs as
  a non-root user, and the job is the only thing in it. Give jobs the same trust you
  would give any self-hosted runner.
- **A compromised controller** can mint runner registrations for its repositories
  and start containers on its host. It needs a token with repository scope and
  access to the Docker socket, so treat it as a privileged service and run it on a
  host you control.
- **A forged webhook** cannot trigger dispatch, because signatures are verified
  before any action is taken.

Out of scope: protecting a host from a job that is deliberately malicious. Self-hosted
runners execute untrusted code by design; isolation is the container's job, and this
image uses the same isolation as any other runner container.
