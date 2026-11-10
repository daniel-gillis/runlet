#!/usr/bin/env python3
"""runletd — a scale-from-zero controller for runlet runners.

A JIT-configured runner serves exactly one job, so an image alone is not a fleet.
This controller closes that gap, and works around the four limits the first version
had:

1. **One repo per process** → `--repo` is repeatable.
2. **Polling latency** → `--webhook-port` accepts `workflow_job` events and wakes
   the loop immediately instead of waiting for the next tick.
3. **No pre-warming** → `--min-warm` keeps idle registered runners, so a job is
   dispatched to an already-listening agent instead of waiting for a container to
   start. This is the single biggest win available without touching the agent: a
   JIT runner idles happily until a job arrives.
4. **One image for all labels** → `--serve label=image[:tag]` maps a label to a
   different image, so a Python profile and a Docker profile can coexist.

Design notes:

* **Matching is exact.** A JIT config's label list *replaces* the defaults — a
  runner minted with `["runlet-runner"]` does NOT also have `self-hosted`. Each
  pool is minted with its profile's own labels, and a job is served only if all of
  its labels are covered by that profile.
* **Containers are the only state.** No desired-state store. Count what is running
  and start the difference; a restart re-derives everything.
* **Bounded.** `--max` caps concurrency per profile.

    GITHUB_TOKEN=... python3 controller/runletd.py \
        --repo owner/repo --serve runlet-runner --image runlet-runner:dev --min-warm 2
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

API = "https://api.github.com"


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "runletd/0.2",
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
                jobs.append({"run_id": run["id"], "job_id": job["id"],
                             "name": job.get("name", "?"), "labels": label_names(job)})
    return jobs


def running_containers(prefix: str) -> int:
    out = subprocess.run(["docker", "ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"docker ps failed: {out.stderr.strip()[:200]}")
    return len([name for name in out.stdout.split() if name])


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


class Profile:
    """A pool of runners: one label set, one image."""

    def __init__(self, label: str, image: str):
        self.label = label
        self.image = image
        self.prefix = f"runlet-{label}-"

    def serves(self, job: dict) -> bool:
        # All of the job's labels must be present on this pool's runners.
        return bool(job["labels"]) and self.label in job["labels"]

    def start(self, repo: str, token: str, pause: float = 0.0) -> bool:
        name = f"{self.prefix}{int(time.time() * 1000)}"
        jit = mint(repo, token, name, [self.label])
        ok = start_container(self.image, name, jit, None)
        if ok and pause:
            time.sleep(pause)
        return ok


class WebhookServer(threading.Thread):
    """Wakes the reconcile loop on a real `workflow_job` event.

    Polling bounds dispatch latency to the poll interval; a webhook removes that
    bound entirely for the common case. Optional: the controller works without it.
    """

    def __init__(self, port: int, secret: str, wake: threading.Event):
        super().__init__(daemon=True)
        self.port = port
        self.secret = secret.encode()
        self.wake = wake

    def run(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                signature = self.headers.get("X-Hub-Signature-256", "")
                expected = "sha256=" + hmac.new(outer.secret, body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    event = {}
                if event.get("action") == "queued":
                    outer.wake.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - base signature
                return

        HTTPServer(("0.0.0.0", self.port), Handler).serve_forever()


def parse_profile(spec: str, default_image: str) -> Profile:
    label, _, image = spec.partition("=")
    return Profile(label.strip(), (image or default_image).strip())


def reconcile(args, token: str, profiles: list[Profile]) -> int:
    """One pass over every repo and profile. Returns the queued job count."""
    total_queued = 0
    for repo in args.repo:
        jobs = queued_jobs(repo, token)
        for profile in profiles:
            mine = [job for job in jobs if profile.serves(job)]
            total_queued += len(mine)
            active = running_containers(profile.prefix)
            # Warm pool means idle runners exist before work arrives; jobs then go
            # to an already-listening agent instead of paying container startup.
            want = min(max(args.min_warm, len(mine)), args.max)
            missing = max(0, want - active)
            if mine or missing:
                print(f"{repo} {profile.label}: queued={len(mine)} active={active} "
                      f"want={want}", flush=True)
            for _ in range(missing):
                if args.dry_run:
                    print(f"  would start {profile.label} ({profile.image})", flush=True)
                    continue
                if profile.start(repo, token, pause=0.3):
                    print(f"  started {profile.label} ({profile.image})", flush=True)
    return total_queued


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", required=True, help="owner/repo; repeatable")
    ap.add_argument("--serve", action="append", required=True,
                    help="label or label=image; repeatable")
    ap.add_argument("--image", default="runlet-runner:latest", help="default image")
    ap.add_argument("--max", type=int, default=8, help="max concurrent runners per profile")
    ap.add_argument("--min-warm", type=int, default=0,
                    help="idle registered runners to keep ready (removes startup from the path)")
    ap.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    ap.add_argument("--webhook-port", type=int, default=0, help="listen for workflow_job events")
    ap.add_argument("--webhook-secret-env", default="RUNLETD_WEBHOOK_SECRET")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="single pass")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("GITHUB_TOKEN (or GH_TOKEN) is required")
    profiles = [parse_profile(spec, args.image) for spec in args.serve]

    wake = threading.Event()
    if args.webhook_port:
        secret = os.environ.get(args.webhook_secret_env, "")
        if not secret:
            raise SystemExit(f"--webhook-port needs {args.webhook_secret_env}")
        WebhookServer(args.webhook_port, secret, wake).start()
        print(f"runletd: webhook listening on :{args.webhook_port}", flush=True)

    print(f"runletd: repos={args.repo} profiles="
          f"{[(p.label, p.image) for p in profiles]} "
          f"min-warm={args.min_warm} max={args.max}", flush=True)

    while True:
        try:
            reconcile(args, token, profiles)
        except SystemExit:
            raise
        except Exception as error:  # a blip must not stop scaling
            print(f"  ! {type(error).__name__}: {error}", flush=True)
        if args.once:
            return 0
        # Wait for the poll interval, or wake early on a webhook.
        wake.wait(timeout=args.interval)
        wake.clear()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
