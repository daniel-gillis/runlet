#!/usr/bin/env python3
"""runletd — a scale-from-zero controller for runlet runners.

A JIT-configured runner serves exactly one job, so on its own an image cannot be
a fleet. This loop closes that gap: it watches for queued jobs that a runlet
runner can serve, mints a single-use configuration per job, starts one container,
and lets the container exit when the job is done.

Design notes:

* **Matching is exact.** A JIT config's label list *replaces* the defaults, so the
  runner is minted with each queued job's own labels. A job is served only if its
  labels are a subset of what we have been told to serve (`--serve`).
* **Containers are the source of truth for capacity.** We never track desired
  state separately; we count running containers by name prefix and start the
  difference. If the controller restarts, it re-derives everything.
* **Bounded.** `--max` caps concurrency so a queue spike cannot exhaust the host.
* **No hidden state.** Every action is an API call or a `docker run` that is
  logged with the reason it was taken.

    GITHUB_TOKEN=... python3 controller/runletd.py \
        --repo daniel-gillis/homelab --serve runlet --image runlet-runner:dev
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "runletd/0.1",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{method} {path} -> {error.code}: {detail}") from None


def label_names(job: dict) -> list[str]:
    """Labels of a job.

    The jobs endpoint returns labels as plain strings; the runners endpoint returns
    objects with a `name`. Accept both rather than assuming one shape.
    """
    result = []
    for label in job.get("labels") or []:
        result.append(label if isinstance(label, str) else str(label.get("name", "")))
    return [name for name in result if name]


def queued_jobs(repo: str, token: str) -> list[dict]:
    """Jobs waiting for a runner, with their labels."""
    runs = api("GET", f"/repos/{repo}/actions/runs?status=queued&per_page=20", token)
    jobs: list[dict] = []
    for run in runs.get("workflow_runs", []):
        for job in api("GET", f"/repos/{repo}/actions/runs/{run['id']}/jobs", token).get("jobs", []):
            if job.get("status") in ("queued", "waiting") and not job.get("runner_name"):
                jobs.append({
                    "run_id": run["id"],
                    "job_id": job["id"],
                    "name": job.get("name", "?"),
                    # The jobs endpoint returns labels as plain strings; the
                    # runners endpoint returns objects. This caught me once.
                    "labels": label_names(job),
                })
    return jobs


def serve(job: dict, wanted: set[str]) -> bool:
    """True when this job's labels are all ones we have declared we serve."""
    return bool(job["labels"]) and set(job["labels"]) <= wanted


def running_containers(prefix: str) -> list[str]:
    out = subprocess.run(["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"docker ps failed: {out.stderr.strip()[:200]}")
    return [name for name in out.stdout.split() if name]


def mint(repo: str, token: str, name: str, labels: list[str]) -> str:
    # The API field is work_folder, not workdir.
    data = api("POST", f"/repos/{repo}/actions/runners/generate-jitconfig", token,
               {"name": name, "runner_group_id": 1, "work_folder": "_work", "labels": labels})
    return data["encoded_jit_config"]


def start_container(image: str, name: str, jit: str, network: str | None) -> bool:
    cmd = ["docker", "run", "-d", "--rm", "--name", name,
           "-e", f"RUNNER_JIT_CONFIG={jit}", "-e", "RUNNER_ALLOW_RUNASROOT=1"]
    if network:
        cmd += ["--network", network]
    cmd.append(image)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        print(f"  ! failed to start {name}: {out.stderr.strip()[:200]}", flush=True)
        return False
    return True


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/repo to serve")
    ap.add_argument("--image", required=True, help="runner image to start")
    ap.add_argument("--serve", action="append", required=True,
                    help="label we are willing to serve; repeatable")
    ap.add_argument("--max", type=int, default=4, help="max concurrent runners")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    ap.add_argument("--network", default="", help="docker network to attach runners to")
    ap.add_argument("--prefix", default="runlet-", help="container name prefix")
    ap.add_argument("--once", action="store_true", help="single pass, for testing")
    ap.add_argument("--dry-run", action="store_true", help="log decisions, start nothing")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("GITHUB_TOKEN (or GH_TOKEN) is required")
    wanted = set(args.serve)

    print(f"runletd: serving {args.repo} for labels {sorted(wanted)} "
          f"(max {args.max}, image {args.image})", flush=True)
    while True:
        try:
            jobs = [job for job in queued_jobs(args.repo, token) if serve(job, wanted)]
            active = running_containers(args.prefix)
            capacity = args.max - len(active)
            if jobs:
                print(f"queued={len(jobs)} active={len(active)} capacity={capacity}", flush=True)
            for job in jobs[:max(0, capacity)]:
                name = f"{args.prefix}{job['job_id']}"
                if any(existing.startswith(name) for existing in active):
                    continue
                if args.dry_run:
                    print(f"  would start {name} for job '{job['name']}' labels={job['labels']}", flush=True)
                    continue
                jit = mint(args.repo, token, name, job["labels"])
                if start_container(args.image, name, jit, args.network or None):
                    print(f"  started {name} for job '{job['name']}' labels={job['labels']}", flush=True)
                    active.append(name)
                    capacity -= 1
                    if capacity <= 0:
                        break
        except SystemExit:
            raise
        except Exception as error:  # keep the loop alive; a blip must not stop scaling
            print(f"  ! {type(error).__name__}: {error}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
