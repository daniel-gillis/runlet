# syntax=docker/dockerfile:1
#
# runlet runner image: the official GitHub Actions agent, built from source with
# the runlet fast-path patch applied.
#
# The patch does not change what the runner can execute — it changes when the
# runner starts work. See ../patches/ and ../docs/measurements.md.

ARG UPSTREAM=310a370bf3605f5b092f8af63b3126627de2a210

# ── build the agent ──────────────────────────────────────────────────────────
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
ARG UPSTREAM
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --quiet https://github.com/actions/runner.git repo \
 && cd repo && git checkout --quiet "$UPSTREAM"
COPY patches/ /src/patches/
# A failing apply is a hard stop: never ship an unpatched image that claims to be
# patched.
RUN cd /src/repo && git apply --verbose /src/patches/runlet-fast-path.patch
RUN cd /src/repo/src && ./dev.sh layout Release linux-x64

# ── runtime ──────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

# The agent ships its own .NET runtime and its own Node (externals/node20), so the
# base only needs what a typical `run:` step expects: a shell, git, and the usual
# archive tooling. Keeping this small is the point — see ARCHITECTURE.md.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      tar \
      gzip \
      unzip \
      libicu72 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/repo/_layout /runner

RUN useradd --create-home --shell /bin/bash runner \
 && mkdir -p /home/runner/_work \
 && chown -R runner:runner /runner /home/runner

USER runner
WORKDIR /runner

# The agent is started with a single-use JIT configuration, so there is no
# registration step and no credential on disk.
ENV RUNNER_JIT_CONFIG=""
ENTRYPOINT ["/bin/bash", "-lc", "exec /runner/run.sh --jitconfig \"$RUNNER_JIT_CONFIG\""]
