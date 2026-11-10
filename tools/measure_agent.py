#!/usr/bin/env python3
"""Measure one agent build from its own perf counters, on the local host.

Starts an agent from a JIT config, dispatches a real GitHub job to it, and reports
the phase timeline between "job message received" and "first console output".

Design rules, each learned from a measurement that lied:

* The perf directory is cleared per run. `read_perf` keeps the first occurrence of
  a phase, so stale marks used to make three "independent" runs identical to the
  millisecond.
* The new run is identified by its creation time relative to the dispatch, not by
  a previously-read run id. The id comparison silently matched an older run and
  then waited forever for a job that had already finished.
* Every wait is bounded and fails loudly. A hung harness must never look like slow
  code.

    python3 tools/measure_agent.py --variant patched --runs 3
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
from datetime import datetime, timezone
from pathlib import Path

ORDER = [
    "MessageReceived_RunnerJobRequest",
    "RefParsed",
    "AckDone",
    "JobFetched",
    "AfterJobMessageParse_RunnerJobRequest",
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


def iso_epoch(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


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


def prune_offline_runners(repo: str) -> int:
    """Remove registrations left behind by earlier interrupted measurements."""
    data = json.loads(run("gh", "api", f"repos/{repo}/actions/runners", check=False) or "{}")
    removed = 0
    for runner in data.get("runners", []):
        if runner.get("status") != "offline":
            continue
        run("gh", "api", "-X", "DELETE",
            f"repos/{args_repo}/actions/runners/{runner['id']}", check=False)
        removed += 1
    return removed


args_repo = "daniel-gillis/homelab"


def wait_online(repo: str, name: str, timeout: int = 45) -> None:
    for _ in range(timeout):
        data = json.loads(run("gh", "api", f"repos/{repo}/actions/runners", check=False) or "{}")
        for runner in data.get("runners", []):
            if runner.get("name") == name and runner.get("status") == "online":
                return
        time.sleep(1)
    raise SystemExit(f"runner {name} never came online — the agent did not register")


def mint(repo: str, name: str, label: str) -> tuple[int, str]:
    data = json.loads(run(
        "gh", "api", "-X", "POST", f"repos/{repo}/actions/runners/generate-jitconfig",
        "-f", f"name={name}", "-F", "runner_group_id=1",
        "-f", f"labels[]={label}", "-f", "work_folder=_work"))
    return data["runner"]["id"], data["encoded_jit_config"]


def cancel_stale_runs(repo: str, workflow: str) -> int:
    """Cancel queued/in-progress runs before measuring.

    An ephemeral runner takes the OLDEST queued job, so a run left behind by an
    earlier interrupted measurement gets served instead of the one just
    dispatched — and the runner then exits, leaving the new run queued forever.
    That looked exactly like a harness hang.
    """
    listing = json.loads(run("gh", "run", "list", "--repo", repo, "--workflow", workflow,
                             "--limit", "20", "--json", "databaseId,status", check=False) or "[]")
    cancelled = 0
    for entry in listing:
        if entry.get("status") in ("queued", "in_progress", "waiting", "pending"):
            run("gh", "run", "cancel", str(entry["databaseId"]), "--repo", repo, check=False)
            cancelled += 1
    if cancelled:
        time.sleep(3)
    return cancelled


def dispatch_new_run(repo: str, workflow: str) -> int:
    """Dispatch and identify the run by creation time, not by a recalled id."""
    dispatched_at = time.time()
    run("gh", "workflow", "run", workflow, "--repo", repo)
    for _ in range(40):
        time.sleep(1)
        listing = json.loads(run("gh", "run", "list", "--repo", repo, "--workflow", workflow,
                                 "--limit", "5", "--json", "databaseId,createdAt", check=False) or "[]")
        for entry in listing:
            if iso_epoch(entry["createdAt"]) >= dispatched_at - 5:
                return entry["databaseId"]
    raise SystemExit("no run appeared within 40s of dispatch — is the workflow/label right?")


def one_run(args, index: int, perf_dir: Path) -> float:
    for stale in ("Runner.perf", "Worker.perf"):
        (perf_dir / stale).unlink(missing_ok=True)

    # Drain the queue BEFORE the agent starts listening. Cancelling after it is
    # online races with the agent: an ephemeral runner takes the first job it is
    # offered, runs a cancelled one, and exits — leaving the fresh run queued.
    stale = cancel_stale_runs(args.repo, args.workflow)
    if stale:
        print(f"  drained {stale} stale run(s) before starting the agent", flush=True)

    name = f"runlet-{args.variant}-{int(time.time())}-{index}"
    runner_id, jit = mint(args.repo, name, args.label)
    log_path = Path(f"/tmp/agent-{args.variant}-{index}.log")
    env = {**os.environ, "RUNNER_PERFLOG": str(perf_dir), "RUNNER_ALLOW_RUNASROOT": "1"}
    with log_path.open("w") as log:
        agent = subprocess.Popen(["./run.sh", "--jitconfig", jit], cwd=args.layout,
                                 env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    try:
        wait_online(args.repo, name)
        run_id = dispatch_new_run(args.repo, args.workflow)
        for _ in range(120):
            status = json.loads(run("gh", "run", "view", str(run_id), "--repo", args.repo,
                                    "--json", "status"))["status"]
            if status == "completed":
                break
            time.sleep(2)
        else:
            raise SystemExit(f"run {run_id} did not complete in 240s")
        time.sleep(2)
    finally:
        try:
            os.killpg(os.getpgid(agent.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        run("gh", "api", "-X", "DELETE", f"repos/{args.repo}/actions/runners/{runner_id}", check=False)

    marks = read_perf(perf_dir)
    if "MessageReceived_RunnerJobRequest" not in marks:
        raise SystemExit(f"no perf marks recorded — see {perf_dir}")
    base = marks["MessageReceived_RunnerJobRequest"]
    print(f"\n[{args.variant} run {index}] chronological")
    previous = 0.0
    for phase in sorted((p for p in ORDER if p in marks), key=lambda p: marks[p]):
        cumulative = marks[phase] - base
        print(f"  {phase[:44]:46} +{(cumulative - previous) * 1000:8.1f} ms  {cumulative * 1000:8.1f} ms")
        previous = cumulative
    print(f"  {'TOTAL assign -> first output':46} {'':8}  {previous * 1000:8.1f} ms")
    return previous


def main(argv) -> int:
    global args_repo
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--repo", default="daniel-gillis/homelab")
    ap.add_argument("--workflow", default="bench-local.yml")
    ap.add_argument("--label", default="runlet-local")
    ap.add_argument("--layout", default="/Users/lab/src/runner/_layout")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args(argv)
    args_repo = args.repo

    pruned = prune_offline_runners(args.repo)
    if pruned:
        print(f"pruned {pruned} offline runner registration(s)")

    perf_dir = Path(f"/tmp/perf-{args.variant}")
    shutil.rmtree(perf_dir, ignore_errors=True)
    perf_dir.mkdir(parents=True)

    totals = sorted(one_run(args, i, perf_dir) for i in range(args.runs))
    print(f"\n{args.variant}: p50 = {totals[len(totals)//2]*1000:.0f} ms over {len(totals)} run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
