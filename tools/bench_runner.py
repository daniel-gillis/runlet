#!/usr/bin/env python3
"""Measure GitHub's job-assignment floor for self-hosted runners.

For each dispatched run we compare three timestamps GitHub exposes:

    run.created_at  ──▶  job.started_at  ──▶  job.completed_at

`created_at → started_at` is the queue/assignment cost: the time from the run
existing to a runner having picked it up. That is the part of cold start we do
not control, so it caps any end-to-end latency claim. `started_at →
completed_at` is the runner's own overhead for a trivial job.

    python3 tools/bench_runner.py --repo OWNER/REPO --runs 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def dispatch(repo: str, workflow: str, ref: str) -> None:
    gh("workflow", "run", workflow, "--repo", repo, "--ref", ref)


def latest_run_id(repo: str, workflow: str) -> int:
    out = gh("run", "list", "--repo", repo, "--workflow", workflow,
             "--limit", "1", "--json", "databaseId")
    runs = json.loads(out) if out.strip() else []
    return runs[0]["databaseId"] if runs else 0


def wait_for(repo: str, run_id: int, timeout: float = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = gh("run", "view", str(run_id), "--repo", repo,
                 "--json", "status,conclusion,createdAt,jobs")
        data = json.loads(out)
        if data["status"] == "completed":
            return data
        time.sleep(2)
    raise SystemExit(f"run {run_id} did not finish in {timeout}s")


def parse(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/repo to measure")
    ap.add_argument("--workflow", default="bench-runner.yml")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    queue: list[float] = []
    execu: list[float] = []
    rows = []
    for i in range(args.runs):
        before = latest_run_id(args.repo, args.workflow)
        dispatch(args.repo, args.workflow, args.ref)
        # Wait for a new run id to appear.
        run_id = before
        for _ in range(30):
            time.sleep(1)
            run_id = latest_run_id(args.repo, args.workflow)
            if run_id != before:
                break
        data = wait_for(args.repo, run_id)
        job = data["jobs"][0]
        # gh exposes camelCase fields; the run carries created_at, the job the
        # assignment and completion timestamps.
        created = parse(data["createdAt"])
        started = parse(job["startedAt"])
        finished = parse(job["completedAt"])
        q_ms = (started - created) * 1000
        e_ms = (finished - started) * 1000
        queue.append(q_ms)
        execu.append(e_ms)
        rows.append({"run_id": run_id, "queue_ms": round(q_ms), "exec_ms": round(e_ms)})
        print(f"run {run_id}: queue={q_ms:7.0f} ms  exec={e_ms:7.0f} ms", flush=True)

    print("\n── GitHub assignment floor ──")
    for name, values in (("queue (created→started)", queue), ("exec (started→done)", execu)):
        print(f"{name:26} p50={pct(values,0.5):7.0f} ms  p95={pct(values,0.95):7.0f} ms  "
              f"max={max(values):7.0f} ms  mean={statistics.mean(values):7.0f} ms")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"runs": rows,
                       "queue_ms": {"p50": pct(queue,0.5), "p95": pct(queue,0.95), "max": max(queue)},
                       "exec_ms": {"p50": pct(execu,0.5), "p95": pct(execu,0.95), "max": max(execu)}}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
