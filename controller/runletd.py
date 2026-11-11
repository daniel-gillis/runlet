#!/usr/bin/env python3
"""runletd: scale-from-zero controller for runlet runners.

Watches GitHub for queued jobs, records them in a ledger, and starts one container
per job with a single-use JIT configuration. State lives in a SQLite file, so a
restart resumes instead of starting over, and a container that dies is retried with
a new fencing token.

    GITHUB_TOKEN=... python3 controller/runletd.py \
        --repo owner/repo --serve runlet-runner --image runlet-runner:0.1.0
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
from pathlib import Path

from ledger import Job, Ledger
from scheduler import Policy, Scheduler

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
        raise RuntimeError(f"{method} {path} -> {error.code}: {detail}") from None


def label_names(job: dict) -> list[str]:
    """Labels of a job, tolerant of both shapes the API returns.

    The jobs endpoint returns plain strings; the runners endpoint returns objects
    with a `name`. Anything else is dropped rather than crashing the loop.
    """
    names: list[str] = []
    for label in job.get("labels") or []:
        if isinstance(label, str) and label:
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


class GitHubSource:
    """Queued jobs across one or more repositories."""

    def __init__(self, repos: list[str], token: str):
        self.repos = repos
        self.token = token

    def queued(self) -> list[Job]:
        jobs: list[Job] = []
        for repo in self.repos:
            runs = api("GET", f"/repos/{repo}/actions/runs?status=queued&per_page=20", self.token)
            for run in runs.get("workflow_runs", []):
                payload = api("GET", f"/repos/{repo}/actions/runs/{run['id']}/jobs", self.token)
                for entry in payload.get("jobs", []):
                    if entry.get("status") not in ("queued", "waiting"):
                        continue
                    if entry.get("runner_name"):
                        continue
                    jobs.append(Job(
                        job_id=str(entry["id"]),
                        repo=repo,
                        name=entry.get("name", "?"),
                        labels=tuple(label_names(entry)),
                        run_id=str(run["id"]),
                        url=entry.get("html_url", ""),
                    ))
        return jobs


class DockerRuntime:
    """Starts one container per runner, minting a JIT configuration first."""

    def __init__(self, token: str, images: dict[str, str], default_image: str,
                 network: str = "", dry_run: bool = False):
        self.token = token
        self.images = images
        self.default_image = default_image
        self.network = network
        self.dry_run = dry_run

    def image_for(self, labels: tuple[str, ...]) -> str:
        for label in labels:
            if label in self.images:
                return self.images[label]
        return self.default_image

    def running(self) -> list[str]:
        out = subprocess.run(["docker", "ps", "--filter", "name=runlet-", "--format", "{{.Names}}"],
                             capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError(f"docker ps failed: {out.stderr.strip()[:200]}")
        return [name for name in out.stdout.split() if name]

    def start(self, runner_name: str, labels: tuple[str, ...], fencing_token: int, repo: str) -> bool:
        image = self.image_for(labels)
        if self.dry_run:
            print(f"  would start {runner_name} ({image}) for labels {list(labels)}", flush=True)
            return True
        try:
            minted = api("POST", f"/repos/{repo}/actions/runners/generate-jitconfig", self.token, {
                "name": runner_name,
                "runner_group_id": 1,
                "work_folder": "_work",
                "labels": list(labels),
            })
        except RuntimeError as error:
            print(f"  ! mint failed for {runner_name}: {error}", flush=True)
            return False
        jit = minted["encoded_jit_config"]

        cmd = ["docker", "run", "-d", "--rm", "--name", runner_name,
               "-e", f"RUNNER_JIT_CONFIG={jit}", "-e", "RUNNER_ALLOW_RUNASROOT=1"]
        if self.network:
            cmd += ["--network", self.network]
        cmd.append(image)
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode:
            print(f"  ! docker run failed for {runner_name}: {out.stderr.strip()[:200]}", flush=True)
            return False
        print(f"  started {runner_name} ({image}) labels={list(labels)}", flush=True)
        return True


class WebhookServer(threading.Thread):
    """Wakes the reconcile loop when a job is queued."""

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

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        HTTPServer(("0.0.0.0", self.port), Handler).serve_forever()


def parse_profile(spec: str, default_image: str) -> tuple[str, str]:
    label, _, image = spec.partition("=")
    return label.strip(), (image or default_image).strip()


def main(argv) -> int:
    if argv and argv[0] == "status":
        import status
        return status.main(argv[1:])

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", required=True, help="owner/repo, repeatable")
    ap.add_argument("--serve", action="append", required=True, help="label or label=image")
    ap.add_argument("--image", default="runlet-runner:latest", help="default image")
    ap.add_argument("--state", default="runlet-ledger.db", help="ledger file")
    ap.add_argument("--max", type=int, default=8, help="max concurrent runners")
    ap.add_argument("--min-warm", type=int, default=0, help="idle runners to keep ready")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--grace", type=float, default=90.0,
                    help="seconds before a missing container counts as a failure")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--network", default="")
    ap.add_argument("--webhook-port", type=int, default=0)
    ap.add_argument("--webhook-secret-env", default="RUNLETD_WEBHOOK_SECRET")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("GITHUB_TOKEN (or GH_TOKEN) is required")

    profiles = dict(parse_profile(spec, args.image) for spec in args.serve)
    warm_labels = tuple(label for label, _ in profiles.items())

    ledger = Ledger(Path(args.state))
    policy = Policy(
        max_runners=args.max,
        min_warm=args.min_warm,
        max_attempts=args.max_attempts,
        grace_seconds=args.grace,
        warm_labels=warm_labels if args.min_warm else (),
        warm_repo=args.repo[0],
    )
    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)

    scheduler = Scheduler(
        ledger=ledger,
        source=GitHubSource(args.repo, token),
        runtime=DockerRuntime(token, profiles, args.image, args.network, args.dry_run),
        policy=policy,
        log=log,
    )

    wake = threading.Event()
    if args.webhook_port:
        secret = os.environ.get(args.webhook_secret_env, "")
        if not secret:
            raise SystemExit(f"--webhook-port needs {args.webhook_secret_env}")
        WebhookServer(args.webhook_port, secret, wake).start()
        print(f"webhook listening on :{args.webhook_port}", flush=True)

    print(f"repos={args.repo} profiles={profiles} max={args.max} "
          f"min-warm={args.min_warm} state={args.state}", flush=True)

    while True:
        try:
            report = scheduler.reconcile()
            if report.changed():
                print(f"observed={report.observed} granted={len(report.granted)} "
                      f"retried={len(report.retried)} completed={len(report.completed)} "
                      f"cancelled={len(report.cancelled)}", flush=True)
        except KeyboardInterrupt:
            break
        except Exception as error:  # a transient failure must not stop scaling
            print(f"  ! {type(error).__name__}: {error}", flush=True)
        if args.once:
            break
        wake.wait(timeout=args.interval)
        wake.clear()

    ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
