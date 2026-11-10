#!/usr/bin/env python3
"""Measure one agent build from its own perf counters, on the local host.

Starts an agent from a JIT config, dispatches a real GitHub job to it, and
reports the phase timeline between "job message received" and "first console
output" — the interval runlet owns and the one the patch targets.

    python3 tools/measure_agent.py --variant patched --runs 3

Requires `gh` authenticated, an agent layout at --layout, and a workflow that
targets an exclusive label (default: runlet-local) so the job cannot land on a
different runner.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ORDER = [
    "MessageReceived_RunnerJobRequest",
    "StartingWorkerProcess",
    "SentJobToWorker_0",
    "JobRequestRenewed_0",
    "WorkerProcessStarted",
    "WorkerJobMessageReceived_0",
    "WorkerJobServerQueueStarted_0",
    "WorkerJobInitialized_0",
    "WorkerJobServerQueueAppendFirstConsoleOutput",
]


def run(*args: str, check: bool = True) -> str:
    out = subprocess.run(args, capture_output=True, text=True)
    if check and out.returncode:
        raise SystemExit(f"{' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout


def parse_ts(text: str) -> float:
    head, frac = text.rstrip("Z").split(".")
    return datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").timestamp() + float("0." + frac[:9])


def read_perf(directory: Path) -> dict[str, float]:
    found: dict[str, float] = {}
    for name in ("Runner.perf", "Worker.perf"):
        path = directory / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if ":" not in line:
                continue
            counter, stamp = line.split(":", 1)
            for phase in ORDER:
                if counter.startswith(phase):
                    try:
                        found.setdefault(phase, parse_ts(stamp))
                    except ValueError:
                        pass
    return found


def latest_run_id(repo: str, workflow: str) -> int:
    out = run("gh", "run", "list", "--repo", repo, "--workflow", workflow,
              "--limit", "1", "--json", "databaseId", check=False)
    runs = json.loads(out) if out.strip() else []
    return runs[0]["databaseId"] if runs else 0


def mint(repo: str, name: str, label: str) -> tuple[int, str]:
    data = json.loads(run(
        "gh", "api", "-X", "POST", f"repos/{repo}/actions/runners/generate-jitconfig",
        "-f", f"name={name}", "-F", "runner_group_id=1",
        "-f", f"labels[]={label}", "-f", "work_folder=_work"))
    return data["runner"]["id"], data["encoded_jit_config"]


def one_run(args, index: int, perf_dir: Path) -> float:
    # Clear the perf files: read_perf keeps the FIRST occurrence of each phase,
    # so stale marks from an earlier run would be reported as this run's timeline
    # (this produced three identical "runs" before it was caught).
    for stale in ("Runner.perf", "Worker.perf"):
        (perf_dir / stale).unlink(missing_ok=True)
    name = f"runlet-{args.variant}-{int(time.time())}-{index}"
    runner_id, jit = mint(args.repo, name, args.label)
    log_path = Path(f"/tmp/agent-{args.variant}-{index}.log")
    env = {**os.environ, "RUNNER_PERFLOG": str(perf_dir), "RUNNER_ALLOW_RUNASROOT": "1"}
    with log_path.open("w") as log:
        agent = subprocess.Popen(["./run.sh", "--jitconfig", jit], cwd=args.layout,
                                 env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    try:
        for _ in range(90):
            time.sleep(1)
            if "Listening for Jobs" in log_path.read_text():
                break
        else:
            raise SystemExit(f"agent never listened; see {log_path}")

        before = latest_run_id(args.repo, args.workflow)
        run("gh", "workflow", "run", args.workflow, "--repo", args.repo)
        run_id = before
        for _ in range(90):
            time.sleep(1)
            current = latest_run_id(args.repo, args.workflow)
            if current and current != before:
                run_id = current
                break
        else:
            raise SystemExit("no new run appeared")

        for _ in range(150):
            status = json.loads(run("gh", "run", "view", str(run_id), "--repo", args.repo,
                                    "--json", "status"))["status"]
            if status == "completed":
                break
            time.sleep(2)
        time.sleep(2)
    finally:
        try:
            os.killpg(os.getpgid(agent.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        run("gh", "api", "-X", "DELETE", f"repos/{args.repo}/actions/runners/{runner_id}", check=False)

    marks = read_perf(perf_dir)
    if "MessageReceived_RunnerJobRequest" not in marks:
        raise SystemExit(f"no perf marks recorded; see {perf_dir}")
    base = marks["MessageReceived_RunnerJobRequest"]
    print(f"\n[{args.variant} run {index}] (chronological order for THIS build)")
    previous = 0.0
    # Sort by actual timestamp: the two variants deliberately differ in whether
    # the lock renewal happens before or after dispatch, so a fixed order would
    # misreport one of them (it produced negative deltas).
    for phase in sorted((p for p in ORDER if p in marks), key=lambda p: marks[p]):
        cumulative = marks[phase] - base
        print(f"  {phase[:44]:46} +{(cumulative - previous) * 1000:7.1f} ms  {cumulative * 1000:8.1f} ms")
        previous = cumulative
    total = previous
    print(f"  {'TOTAL assign -> first output':46} {'':7}  {total * 1000:8.1f} ms")
    return total


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--repo", default="daniel-gillis/homelab")
    ap.add_argument("--workflow", default="bench-local.yml")
    ap.add_argument("--label", default="runlet-local")
    ap.add_argument("--layout", default="/Users/lab/src/runner/_layout")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args(argv)

    perf_dir = Path(f"/tmp/perf-{args.variant}")
    shutil.rmtree(perf_dir, ignore_errors=True)
    perf_dir.mkdir(parents=True)

    totals = [one_run(args, i, perf_dir) for i in range(args.runs)]
    totals.sort()
    print(f"\n{args.variant}: p50 = {totals[len(totals)//2]*1000:.0f} ms over {len(totals)} run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
