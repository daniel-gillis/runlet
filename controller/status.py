#!/usr/bin/env python3
"""Read the ledger and explain what the scheduler is doing.

Answers three questions in plain language: what is running now, what is waiting,
and what finished recently. Reads the SQLite file directly, so it works whether or
not the scheduler process is alive.

    python3 controller/status.py --state runlet-ledger.db
    python3 controller/status.py --state runlet-ledger.db --short
    python3 controller/status.py --state runlet-ledger.db --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ledger import Ledger


def duration(seconds: float) -> str:
    """Compact duration for humans: 8s, 3m12s, 1h04m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def live_containers(prefix: str = "runlet-") -> set[str]:
    """Names of live runners, from the local docker daemon.

    Raises FileNotFoundError when there is no docker CLI, which is the normal case
    inside the cluster; callers use the Kubernetes runtime there instead.
    """
    out = subprocess.run(["docker", "ps", "--filter", f"name={prefix}",
                          "--format", "{{.Names}}"], capture_output=True, text=True)
    if out.returncode:
        return set()
    return {name for name in out.stdout.split() if name}


def kubernetes_containers(namespace: str, image: str, prefix: str = "runlet-") -> set[str]:
    from kubernetes_runtime import KubernetesRuntime
    return set(KubernetesRuntime(namespace, image, prefix=prefix).running())


def collect(ledger: Ledger, now: float | None = None, live: set[str] | None = None) -> dict:
    """Everything the renderer needs, as plain data."""
    now = now if now is not None else time.time()
    if live is None:
        live = live_containers()

    running = []
    for lease in ledger.active_leases():
        if lease.job_id is None:
            running.append({
                "kind": "warm",
                "runner": lease.runner_name,
                "job": "warm runner, waiting for a job",
                "repo": "",
                "elapsed": now - lease.granted_at,
                "attempt": 0,
                "container_present": lease.runner_name in live,
            })
            continue
        row = ledger.db.execute(
            "SELECT repo, name, url FROM jobs WHERE job_id=?", (lease.job_id,)).fetchone()
        running.append({
            "kind": "job",
            "runner": lease.runner_name,
            "job": row["name"] if row else lease.job_id,
            "repo": row["repo"] if row else "",
            "url": row["url"] if row else "",
            "elapsed": now - lease.granted_at,
            "attempt": lease.fencing_token,
            "container_present": lease.runner_name in live,
        })

    queued = []
    for job in ledger.queued():
        row = ledger.db.execute(
            "SELECT first_seen, url FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        queued.append({
            "job": job.name,
            "repo": job.repo,
            "labels": list(job.labels),
            "waiting": now - (row["first_seen"] if row else now),
            "url": row["url"] if row else "",
        })

    recent = []
    for row in ledger.recent(limit=15, since=now - 6 * 3600):
        recent.append({
            "job": row["name"],
            "repo": row["repo"],
            "state": row["state"],
            "attempts": row["attempts"],
            "finished_ago": now - row["updated_at"],
            "took": row["updated_at"] - row["first_seen"],
        })

    return {"running": running, "queued": queued, "recent": recent,
            "counts": {
                "running": sum(1 for entry in running if entry["kind"] == "job"),
                "warm": sum(1 for entry in running if entry["kind"] == "warm"),
                "queued": len(queued),
                "recent": len(recent),
            }}


def render(state: dict, max_runners: int | None = None) -> str:
    lines: list[str] = []
    counts = state["counts"]

    if counts["running"] or counts["warm"]:
        capacity = f" of {max_runners}" if max_runners else ""
        lines.append(f"RUNNING {counts['running']}{capacity}"
                     + (f"  (+{counts['warm']} warm)" if counts["warm"] else ""))
        for entry in state["running"]:
            where = f"{entry['repo']}" if entry["repo"] else ""
            missing = "" if entry["container_present"] else "  [container gone, will retry]"
            retry = f"  attempt {entry['attempt']}" if entry["attempt"] > 1 else ""
            lines.append(f"  {duration(entry['elapsed']):>7}  {entry['job'][:40]:<40} "
                         f"{where:<28}{retry}{missing}")
    else:
        lines.append("RUNNING nothing")

    if state["queued"]:
        lines.append("")
        lines.append(f"QUEUED {counts['queued']}")
        for entry in state["queued"]:
            labels = ",".join(entry["labels"])
            lines.append(f"  {duration(entry['waiting']):>7}  {entry['job'][:40]:<40} "
                         f"{entry['repo']:<28} {labels}")
    else:
        lines.append("QUEUED nothing")

    if state["recent"]:
        lines.append("")
        lines.append("RECENT")
        for entry in state["recent"][:8]:
            verb = "ok" if entry["state"] == "done" else "cancelled"
            tries = f", {entry['attempts']} retries" if entry["attempts"] else ""
            lines.append(f"  {duration(entry['finished_ago']):>7} ago  {entry['job'][:36]:<36} "
                         f"{entry['repo']:<26} {verb}, took {duration(entry['took'])}{tries}")
    return "\n".join(lines)


def short(state: dict) -> str:
    counts = state["counts"]
    parts = [f"{counts['running']} running"]
    if counts["warm"]:
        parts.append(f"{counts['warm']} warm")
    parts.append(f"{counts['queued']} queued")
    if counts["recent"]:
        parts.append(f"{counts['recent']} finished in the last 6h")
    return ", ".join(parts)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="runlet-ledger.db")
    ap.add_argument("--max", type=int, default=None, help="show capacity as 'N of MAX'")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--short", action="store_true", help="one line")
    ap.add_argument("--runtime", choices=["docker", "kubernetes"], default="docker",
                    help="where runners are running; kubernetes works in-cluster")
    args = ap.parse_args(argv)

    path = Path(args.state)
    if not path.exists():
        print(f"no ledger at {path}. Start the scheduler with --state {path}", file=sys.stderr)
        return 1

    live = None
    if args.runtime == "kubernetes":
        live = kubernetes_containers(os.environ.get("RUNLET_NAMESPACE", "default"),
                                     os.environ.get("RUNLET_IMAGE", "ghcr.io/daniel-gillis/runlet-runner:0.1.0"))

    ledger = Ledger(path)
    try:
        state = collect(ledger, live=live)
    finally:
        ledger.close()

    if args.json:
        print(json.dumps(state, indent=2))
    elif args.short:
        print(short(state))
    else:
        print(render(state, args.max))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
