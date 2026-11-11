#!/usr/bin/env python3
"""Prometheus metrics for the scheduler.

Every metric is derived from the ledger, so the values survive a restart. Only the
reconcile counters live in memory and reset when the process does, which is what
Prometheus expects of a counter.

Enabled with `--metrics-port`. Endpoints:

    /metrics   Prometheus text format
    /healthz   200 when the last pass succeeded
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from ledger import Ledger


class PassStats:
    """Counters and timing for the reconcile loop, updated by the scheduler."""

    def __init__(self):
        self.passes = 0
        self.failures = 0
        self.last_pass_at = 0.0
        self.last_duration = 0.0
        self.last_error = ""
        self._lock = threading.Lock()

    def record(self, seconds: float, error: str = "") -> None:
        with self._lock:
            self.passes += 1
            self.last_pass_at = time.time()
            self.last_duration = seconds
            if error:
                self.failures += 1
                self.last_error = error
            else:
                self.last_error = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "passes": self.passes,
                "failures": self.failures,
                "last_pass_at": self.last_pass_at,
                "last_duration": self.last_duration,
                "last_error": self.last_error,
            }


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def collect(ledger: Ledger, stats: PassStats, running: set[str] | None = None,
            now: float | None = None) -> dict:
    """Gather every metric as plain data, so the renderer stays trivial."""
    now = now if now is not None else time.time()

    by_state = {row["state"]: row["n"] for row in ledger.db.execute(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}

    queued = []
    for job in ledger.queued():
        row = ledger.db.execute("SELECT first_seen FROM jobs WHERE job_id=?",
                                (job.job_id,)).fetchone()
        queued.append({
            "repo": job.repo, "job": job.name,
            "waiting": now - (row["first_seen"] if row else now),
        })

    running_jobs = []
    missing = 0
    warm = 0
    for lease in ledger.active_leases():
        if lease.job_id is None:
            warm += 1
        else:
            row = ledger.db.execute("SELECT repo, name FROM jobs WHERE job_id=?",
                                    (lease.job_id,)).fetchone()
            running_jobs.append({
                "repo": row["repo"] if row else "",
                "job": row["name"] if row else lease.job_id,
                "runner": lease.runner_name,
                "elapsed": now - lease.granted_at,
            })
        if running is not None and lease.runner_name not in running:
            missing += 1

    attempts = ledger.db.execute("SELECT COALESCE(SUM(attempts), 0) AS n FROM jobs").fetchone()["n"]
    oldest = ledger.db.execute(
        "SELECT COALESCE(MIN(first_seen), 0) AS t FROM jobs WHERE state='queued'").fetchone()["t"]

    return {
        "by_state": by_state,
        "queued": queued,
        "running_jobs": running_jobs,
        "warm": warm,
        "missing": missing,
        "attempts": attempts,
        "oldest_queued_age": (now - oldest) if oldest else 0.0,
        "stats": stats.snapshot(),
    }


def render(data: dict) -> str:
    out: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")

    family("runlet_jobs", "gauge", "Jobs in the ledger by state.")
    for state in ("queued", "running", "done", "cancelled"):
        out.append(f'runlet_jobs{{state="{state}"}} {data["by_state"].get(state, 0)}')

    family("runlet_queued_jobs", "gauge", "Jobs waiting for a runner.")
    out.append(f"runlet_queued_jobs {len(data['queued'])}")

    family("runlet_queued_oldest_seconds", "gauge",
           "Age of the longest-waiting queued job.")
    out.append(f"runlet_queued_oldest_seconds {data['oldest_queued_age']:.1f}")

    family("runlet_job_wait_seconds", "gauge", "Seconds a queued job has been waiting.")
    for entry in data["queued"]:
        out.append(f'runlet_job_wait_seconds{{repo="{label(entry["repo"])}",'
                   f'job="{label(entry["job"])}"}} {entry["waiting"]:.1f}')

    family("runlet_runners", "gauge", "Runners by kind.")
    out.append(f'runlet_runners{{kind="job"}} {len(data["running_jobs"])}')
    out.append(f'runlet_runners{{kind="warm"}} {data["warm"]}')

    family("runlet_job_running_seconds", "gauge", "Seconds a leased job has been running.")
    for entry in data["running_jobs"]:
        out.append(f'runlet_job_running_seconds{{repo="{label(entry["repo"])}",'
                   f'job="{label(entry["job"])}",runner="{label(entry["runner"])}"}} '
                   f'{entry["elapsed"]:.1f}')

    family("runlet_runner_missing", "gauge",
           "Active leases whose container is no longer running.")
    out.append(f"runlet_runner_missing {data['missing']}")

    family("runlet_job_attempts_total", "counter",
           "Retry attempts recorded across all jobs.")
    out.append(f"runlet_job_attempts_total {data['attempts']}")

    stats = data["stats"]
    family("runlet_reconcile_total", "counter", "Reconcile passes since start.")
    out.append(f"runlet_reconcile_total {stats['passes']}")

    family("runlet_reconcile_failures_total", "counter", "Reconcile passes that raised.")
    out.append(f"runlet_reconcile_failures_total {stats['failures']}")

    family("runlet_reconcile_duration_seconds", "gauge", "Duration of the last pass.")
    out.append(f"runlet_reconcile_duration_seconds {stats['last_duration']:.4f}")

    family("runlet_last_reconcile_timestamp_seconds", "gauge",
           "Unix time of the last pass; rising values mean the loop is alive.")
    out.append(f"runlet_last_reconcile_timestamp_seconds {stats['last_pass_at']:.0f}")

    return "\n".join(out) + "\n"


class MetricsServer(threading.Thread):
    def __init__(self, port: int, ledger_path: str, stats: PassStats):
        super().__init__(daemon=True)
        self.port = port
        self.ledger_path = ledger_path
        self.stats = stats

    def run(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path.split("?")[0] == "/healthz":
                    healthy = outer.stats.snapshot()["passes"] > 0 and not \
                        outer.stats.snapshot()["last_error"]
                    body = b"ok\n" if healthy else b"stale\n"
                    self.send_response(200 if healthy else 503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.split("?")[0] != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    ledger = Ledger(outer.ledger_path)
                    try:
                        body = render(collect(ledger, outer.stats)).encode()
                    finally:
                        ledger.close()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                except Exception as error:  # never break the scrape itself
                    body = f"# error collecting metrics: {error}\n".encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        HTTPServer(("0.0.0.0", self.port), Handler).serve_forever()
