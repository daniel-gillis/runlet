# Security policy

## Supported versions

The latest release receives fixes. The project ships a container image and a patch series; upgrading means pulling a new image or rebasing the patch.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/daniel-gillis/runlet/security/advisories/new). Do not open a public issue for anything exploitable.

Expect an acknowledgement within 72 hours. Fixes ship as a patch release with an advisory describing the impact and the affected versions.

## Guarantees

1. No long-lived credentials. The image starts from a single-use JIT configuration in an environment variable, writes nothing to disk, and the credential is invalid after one job.
2. The controller holds one token, read from `GITHUB_TOKEN` or `GH_TOKEN`. It needs repository scope and is never written to disk.
3. Webhook requests are verified. The optional listener checks `X-Hub-Signature-256` with HMAC-SHA256 and returns 401 for anything else.
4. The patch is bounded. CI fails if it touches any file other than `src/Runner.Listener/JobDispatcher.cs` and `src/Runner.Listener/Runner.cs`.
5. The agent is otherwise unmodified. The image builds from a pinned upstream commit and applies the patch with `git apply`, which fails the build if the patch does not apply.

## Threat model

A compromised runner container can read anything the job can read. It runs as a non-root user and contains only the job. Give jobs the same trust you give any self-hosted runner.

A compromised controller can mint runner registrations for its repositories and start containers on its host. It needs a repository-scoped token and access to the Docker socket, so run it on a host you control and treat it as a privileged service.

A forged webhook cannot trigger dispatch, because signatures are checked before any action is taken.

Out of scope: protecting a host from a job that is deliberately hostile. Self-hosted runners execute untrusted code by design. Isolation is the container's job, and this image uses the same isolation as any other runner container.
