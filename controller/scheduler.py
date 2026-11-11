#!/usr/bin/env python3
"""Level-triggered scheduler.

Each pass compares what the ledger says should be running with what the runtime
actually has, and converges. Nothing is driven by an event that could be missed,
so a scheduler that was offline for an hour finds the same queue on restart.

The source and the runtime are injected, which is what makes the logic testable
without GitHub or Docker.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from ledger import Job, Lease, Ledger


class Source(Protocol):
    """Where queued jobs come from."""

    def queued(self) -> list[Job]:
        ...


class Runtime(Protocol):
    """Where runners run."""

    def running(self) -> list[str]:
        ...

    def start(self, runner_name: str, labels: tuple[str, ...], fencing_token: int,
              repo: str) -> bool:
        ...


@dataclass
class Policy:
    max_runners: int = 8
    min_warm: int = 0
    max_attempts: int = 3
    # Labels for warm runners. Empty disables prewarming, because a runner with
    # no label can never match a job.
    warm_labels: tuple[str, ...] = ()
    # Repository used to mint warm runners. Required when warm_labels is set.
    warm_repo: str = ""
    # A lease with no matching container is expired only after this long, so a
    # container that is still being created is not counted as failed.
    grace_seconds: float = 90.0


@dataclass
class Report:
    observed: int = 0
    granted: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def changed(self) -> bool:
        return bool(self.granted or self.expired or self.retried or self.completed or self.cancelled)


class Scheduler:
    def __init__(self, ledger: Ledger, source: Source, runtime: Runtime, policy: Policy,
                 log=None):
        self.ledger = ledger
        self.source = source
        self.runtime = runtime
        self.policy = policy
        # Logging is injected so tests can assert on it and the entrypoint can
        # decide the format. Default is silent.
        self.log = log or (lambda message: None)

    def _describe(self, job_id: str) -> str:
        """Human name for a job, used in every log line."""
        row = self.ledger.db.execute(
            "SELECT repo, name FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return job_id
        return f'{row["repo"]} "{row["name"]}"' if row["name"] else row["repo"]

    def reconcile(self, now: float | None = None) -> Report:
        now = now if now is not None else time.time()
        report = Report()

        upstream = self.source.queued()
        report.observed = len(upstream)
        for job in upstream:
            self.ledger.observe(job, now=now)
        waiting = {job.job_id for job in upstream}

        self._expire_leases(waiting, report, now)
        self._cancel_absent_jobs(waiting, report, now)
        self._dispatch(report, now)
        return report

    # ── convergence steps ────────────────────────────────────────────────────
    def _expire_leases(self, waiting: set[str], report: Report, now: float) -> None:
        """End leases whose container is gone.

        The job's upstream state decides what the exit meant. Still queued means
        the runner never took the job, so it is retried. No longer queued means the
        runner took it, so the job is done. Guessing here would either retry
        completed work or lose failed work.
        """
        live = set(self.runtime.running())
        for lease in self.ledger.active_leases():
            if lease.runner_name in live:
                continue

            if lease.job_id is None:
                # Prewarm lease: a missing container only means the pool shrank.
                self.ledger.end_lease(lease.lease_id, "completed", now=now)
                report.expired.append(lease.lease_id)
                continue

            if now - lease.granted_at < self.policy.grace_seconds:
                continue

            self.ledger.end_lease(lease.lease_id, "expired", now=now)
            report.expired.append(lease.lease_id)

            if lease.job_id in waiting:
                attempts = self.ledger.bump_attempts(lease.job_id)
                if attempts >= self.policy.max_attempts:
                    self.ledger.set_state(lease.job_id, "cancelled", now=now)
                    report.cancelled.append(lease.job_id)
                    self.log(f'giving up on {self._describe(lease.job_id)} after {attempts} '
                             f'attempts: the runner exited without taking the job')
                else:
                    self.ledger.set_state(lease.job_id, "queued", now=now)
                    report.retried.append(lease.job_id)
                    self.log(f'retrying {self._describe(lease.job_id)} '
                             f'(attempt {attempts + 1} of {self.policy.max_attempts}): '
                             f'the runner exited without taking the job')
            else:
                self.ledger.set_state(lease.job_id, "done", now=now)
                report.completed.append(lease.job_id)
                elapsed = now - lease.granted_at
                self.log(f'finished {self._describe(lease.job_id)} after {elapsed:.0f}s '
                         f'({lease.runner_name})')

    def _cancel_absent_jobs(self, waiting: set[str], report: Report, now: float) -> None:
        """Mark jobs that are queued locally but no longer queued upstream."""
        leased = {lease.job_id for lease in self.ledger.active_leases()}
        for job in self.ledger.queued():
            if job.job_id in waiting or job.job_id in leased:
                continue
            self.ledger.set_state(job.job_id, "cancelled", now=now)
            report.cancelled.append(job.job_id)
            self.log(f'cancelled {self._describe(job.job_id)}: it is no longer queued upstream')

    def _dispatch(self, report: Report, now: float) -> None:
        """Start runners for queued jobs, then top up the warm pool."""
        active = self.ledger.active_leases()
        job_leases = [lease for lease in active if lease.job_id is not None]
        prewarm = [lease for lease in active if lease.job_id is None]
        capacity = self.policy.max_runners - len(active)

        for job in self.ledger.queued():
            if self.ledger.attempts(job.job_id) >= self.policy.max_attempts:
                self.ledger.set_state(job.job_id, "cancelled", now=now)
                report.cancelled.append(job.job_id)
                self.log(f'giving up on {self._describe(job.job_id)}: attempt limit reached')
                continue
            if capacity <= 0:
                break
            if prewarm:
                # An idle runner with matching labels will take this job. Convert
                # one prewarm lease into a job lease instead of starting another.
                warm = prewarm.pop()
                self.ledger.assign_prewarm(warm.lease_id, job.job_id, now=now)
                report.granted.append(warm.lease_id)
                self.log(f'dispatching {self._describe(job.job_id)} to a warm runner '
                         f'({warm.runner_name})')
                continue

            if self._grant(job, now):
                report.granted.append(job.job_id)
                capacity -= 1

        missing = self.policy.min_warm - len([lease for lease in self.ledger.active_leases()
                                              if lease.job_id is None])
        for _ in range(max(0, min(missing, capacity))):
            if self._prewarm(now):
                report.granted.append("prewarm")

    # ── lease creation ───────────────────────────────────────────────────────
    def _grant(self, job: Job, now: float) -> bool:
        token = self.ledger.next_fencing_token(job.job_id)
        runner_name = f"runlet-{job.job_id}-{token}"
        lease = Lease(
            lease_id=str(uuid.uuid4()),
            job_id=job.job_id,
            runner_name=runner_name,
            fencing_token=token,
            state="active",
            granted_at=now,
        )
        if not self.runtime.start(runner_name, job.labels, token, job.repo):
            self.log(f'could not start a runner for {self._describe(job.job_id)}')
            return False
        self.ledger.grant(lease)
        self.log(f'dispatching {self._describe(job.job_id)} to {runner_name} '
                 f'(labels {list(job.labels)}, attempt {token})')
        return True

    def _prewarm(self, now: float) -> bool:
        runner_name = f"runlet-warm-{uuid.uuid4().hex[:8]}"
        if not self.policy.warm_labels or not self.policy.warm_repo:
            return False
        if not self.runtime.start(runner_name, self.policy.warm_labels, 0, self.policy.warm_repo):
            return False
        self.ledger.grant(
            Lease(str(uuid.uuid4()), None, runner_name, 0, "active", now)
        )
        return True
