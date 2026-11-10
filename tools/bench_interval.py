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


def runner_pod(namespace: str) -> str:
    out = sh("kubectl", "-n", namespace, "get", "pods",
             "-l", "app.kubernetes.io/name=github-runner",
             "--no-headers", "-o", "custom-columns=:metadata.name")
    pods = [p for p in out.split() if p]
    if not pods:
        raise SystemExit("no runner pods found")
    return pods[0]


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
    for line in logs.splitlines():
        m = EPOCH_RE.search(line)
        if m:
            return float(m.group(1))
    raise SystemExit(f"no RUNLET_STEP_EPOCH in run {run_id}")


def agent_assign_epoch(namespace: str, pod: str) -> float:
    """Timestamp of the most recent 'Running job' line in the runner log."""
    logs = sh("kubectl", "-n", namespace, "logs", pod, "--timestamps",
              "--since=15m", "--tail=400")
    found = None
    for line in logs.splitlines():
        m = LOG_TS_RE.match(line)
        if m:
            found = parse_ts(m.group(1))
    if found is None:
        raise SystemExit("no 'Running job' line in the runner log")
    return found


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

    pod = runner_pod(args.namespace)
    print(f"runner pod: {pod}")
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
        t_step = step_epoch(args.repo, run_id)
        t_assign = agent_assign_epoch(args.namespace, pod)
        delta_ms = (t_step - t_assign) * 1000
        # A negative or enormous delta means we paired the wrong log line.
        if -500 < delta_ms < 60_000:
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
