#!/usr/bin/env python3
"""Measure runlet's target interval at millisecond resolution.

The interval of interest is **job assigned to a runner -> first step executing**.
The GitHub REST API only reports whole seconds, which is useless for a 200-400 ms
target. Instead this correlates two timestamps taken from the *same* clock — the
pod's:

  T_assign  the runner agent's own log line "Running job: …", as reported by
            `kubectl logs --timestamps` (containerd timestamps are nanosecond)
  T_step    `RUNLET_STEP_EPOCH`, printed by the first step via `date +%s.%N`

Both are read from inside the same container, so the delta contains no clock skew.

    python3 tools/bench_interval.py --runs 10
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time

EPOCH_RE = re.compile(r"RUNLET_STEP_EPOCH=(\d+\.\d+)")
LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z .*Running job: (\S+)")


def sh(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{' '.join(args)}: {result.stderr.strip()[:300]}")
    return result.stdout


# Match a real pod name, not the echoed command line "runner_host=$(hostname)".
HOST_RE = re.compile(r"runner_host=(github-runner-[a-z0-9-]+)")


def runner_pod(namespace: str, repo: str, run_id: int) -> str:
    """The pod that actually served this run.

    Picking the first pod is wrong: there are two runners, and reading the other
    one's log yields a stale 'Running job' line — which is what produced deltas of
    hundreds of seconds. The workflow prints its hostname, so ask it.
    """
    logs = sh("gh", "run", "view", str(run_id), "--repo", repo, "--log")
    matches = HOST_RE.findall(logs)
    match = matches[-1] if matches else None
    if not match:
        raise SystemExit(f"run {run_id}: no runner_host in the job log")
    pod = match
    available = sh("kubectl", "-n", namespace, "get", "pods",
                   "-l", "app.kubernetes.io/name=github-runner",
                   "--no-headers", "-o", "custom-columns=:metadata.name").split()
    if pod not in available:
        raise SystemExit(f"run {run_id}: host {pod} is not a current runner pod ({available})")
    return pod


def parse_ts(value: str) -> float:
    # 2026-11-10T04:12:33.123456789Z -> epoch seconds
    from datetime import datetime, timezone
    head, frac = value.split(".")
    base = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return base + float("0." + frac[:9])


def dispatch(repo: str, workflow: str) -> None:
    sh("gh", "workflow", "run", workflow, "--repo", repo)


def latest_run(repo: str, workflow: str) -> int:
    out = sh("gh", "run", "list", "--repo", repo, "--workflow", workflow,
             "--limit", "1", "--json", "databaseId")
    runs = json.loads(out) if out.strip() else []
    return runs[0]["databaseId"] if runs else 0


def wait_done(repo: str, run_id: int, timeout: float = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = sh("gh", "run", "view", str(run_id), "--repo", repo, "--json", "status")
        if json.loads(out)["status"] == "completed":
            return
        time.sleep(2)
    raise SystemExit(f"run {run_id} did not complete")


def step_epoch(repo: str, run_id: int) -> float:
    logs = sh("gh", "run", "view", str(run_id), "--repo", repo, "--log")
    stamps = [float(m.group(1)) for m in (EPOCH_RE.search(l) for l in logs.splitlines()) if m]
    if not stamps:
        raise SystemExit(f"no RUNLET_STEP_EPOCH in run {run_id}")
    return stamps[-1]


def agent_assign_epoch(namespace: str, pod: str, t_step: float) -> float:
    """The 'Running job' line belonging to this run.

    Chosen by proximity to the step stamp (within 120s), not by "the last line",
    so a stale entry from an earlier job on a long-lived pod cannot be paired.
    """
    logs = sh("kubectl", "-n", namespace, "logs", pod, "--timestamps",
              "--since=30m", "--tail=800")
    candidates = []
    for line in logs.splitlines():
        m = LOG_TS_RE.match(line)
        if m:
            candidates.append(parse_ts(m.group(1)))
    close = [c for c in candidates if 0 <= t_step - c <= 120]
    if not close:
        raise SystemExit(f"no 'Running job' line within 120s of the step on {pod}")
    return max(close)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="daniel-gillis/homelab")
    ap.add_argument("--workflow", default="bench-runner.yml")
    ap.add_argument("--namespace", default="homelab")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    print("resolving runner pod per run (two runners, job may land on either)")
    deltas: list[float] = []
    rows = []
    for _ in range(args.runs):
        before = latest_run(args.repo, args.workflow)
        dispatch(args.repo, args.workflow)
        run_id = before
        for _ in range(40):
            time.sleep(1)
            current = latest_run(args.repo, args.workflow)
            if current != before:
                run_id = current
                break
        wait_done(args.repo, run_id)
        # Give the agent a moment to flush its log line.
        time.sleep(1)
        pod = runner_pod(args.namespace, args.repo, run_id)
        t_step = step_epoch(args.repo, run_id)
        t_assign = agent_assign_epoch(args.namespace, pod, t_step)
        delta_ms = (t_step - t_assign) * 1000
        # A negative or enormous delta means we paired the wrong log line.
        if 0 <= delta_ms < 5_000:
            deltas.append(delta_ms)
            rows.append({"run_id": run_id, "assign_to_step_ms": round(delta_ms, 1)})
            print(f"run {run_id}: assign→first-step = {delta_ms:7.1f} ms", flush=True)
        else:
            print(f"run {run_id}: discarded (delta {delta_ms:.0f} ms — log line pairing failed)", flush=True)

    if not deltas:
        raise SystemExit("no usable measurements")
    print("\n── job assigned → first step ──")
    print(f"n={len(deltas)}  p50={pct(deltas,0.5):7.1f} ms  p95={pct(deltas,0.95):7.1f} ms  "
          f"max={max(deltas):7.1f} ms  mean={statistics.mean(deltas):7.1f} ms")
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"runs": rows,
                       "p50": pct(deltas, 0.5), "p95": pct(deltas, 0.95), "max": max(deltas)},
                      fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
