#!/usr/bin/env python3
"""Split the "queue time" into the two intervals GitHub exposes.

    run.created_at  ->  job.created_at   workflow setup: YAML parsed, jobs created
    job.created_at  ->  job.started_at   assignment: broker message, runner pickup

The distinction matters because only the second one involves a runner at all.

    python3 tools/bench_queue.py --repo OWNER/REPO --runs 5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone


def gh(*args: str) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"gh {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout


def epoch(text: str | None) -> float | None:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def latest_run(repo: str, workflow: str) -> int:
    out = gh("run", "list", "--repo", repo, "--workflow", workflow,
             "--limit", "1", "--json", "databaseId")
    runs = json.loads(out) if out.strip() else []
    return runs[0]["databaseId"] if runs else 0


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/repo to measure")
    ap.add_argument("--workflow", default="bench-local.yml")
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args(argv)

    setup: list[float] = []
    assign: list[float] = []

    for _ in range(args.runs):
        before = latest_run(args.repo, args.workflow)
        gh("workflow", "run", args.workflow, "--repo", args.repo)

        run_id = before
        for _ in range(60):
            time.sleep(0.5)
            current = latest_run(args.repo, args.workflow)
            if current and current != before:
                run_id = current
                break
        else:
            raise SystemExit("no new run appeared")

        run_created = None
        for _ in range(240):
            run = json.loads(gh("api", f"repos/{args.repo}/actions/runs/{run_id}"))
            run_created = epoch(run["created_at"])
            jobs = json.loads(gh("api", f"repos/{args.repo}/actions/runs/{run_id}/jobs"))["jobs"]
            job = jobs[0] if jobs else {}
            job_created = epoch(job.get("created_at"))
            job_started = epoch(job.get("started_at"))
            if job_started:
                break
            time.sleep(0.5)

        if not (run_created and job_created and job_started):
            print(f"run {run_id}: incomplete timestamps, skipping", flush=True)
            continue

        a = job_created - run_created
        b = job_started - job_created
        setup.append(a)
        assign.append(b)
        print(f"run {run_id}: workflow setup={a:5.1f}s  assignment={b:5.1f}s  "
              f"total={job_started - run_created:5.1f}s", flush=True)

        # Let the job finish so the next dispatch is clean.
        for _ in range(120):
            status = json.loads(gh("run", "view", str(run_id), "--repo", args.repo,
                                   "--json", "status"))["status"]
            if status == "completed":
                break
            time.sleep(1)

    if setup:
        def median(values):
            return sorted(values)[len(values) // 2]
        print(f"\nworkflow setup: median {median(setup):.1f}s  "
              f"range {min(setup):.1f}-{max(setup):.1f}s")
        print(f"assignment:     median {median(assign):.1f}s  "
              f"range {min(assign):.1f}-{max(assign):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
