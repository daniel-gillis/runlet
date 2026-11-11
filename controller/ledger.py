#!/usr/bin/env python3
"""Durable job and lease ledger.

The scheduler keeps no state in memory. Everything it needs between passes lives
here, so a restart resumes from the queue instead of from nothing.

Two tables:

`jobs`     one row per upstream job id, with the labels it needs and its state.
`leases`   one row per runner started for a job, with a fencing token and a state.

A lease is the record that a container was started for a job. Fencing tokens are
monotonic per job, so a stale container can be told apart from the current one
after a retry.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    name        TEXT NOT NULL,
    labels      TEXT NOT NULL,          -- json array
    state       TEXT NOT NULL,          -- queued | running | done | cancelled
    attempts    INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id      TEXT PRIMARY KEY,
    job_id        TEXT REFERENCES jobs(job_id),   -- null for prewarm leases
    runner_name   TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    state         TEXT NOT NULL,        -- active | completed | expired
    granted_at    REAL NOT NULL,
    ended_at      REAL
);

CREATE INDEX IF NOT EXISTS leases_active ON leases(state) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    repo: str
    name: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Lease:
    lease_id: str
    # None for a prewarm lease: a runner that is listening but not yet tied to a job.
    job_id: str | None
    runner_name: str
    fencing_token: int
    state: str
    granted_at: float


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ── jobs ────────────────────────────────────────────────────────────────
    def observe(self, job: Job, now: float | None = None) -> bool:
        """Record a job seen upstream. Returns True when it is new."""
        now = now if now is not None else time.time()
        row = self.db.execute("SELECT state FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO jobs (job_id, repo, name, labels, state, first_seen, updated_at)"
                " VALUES (?,?,?,?,'queued',?,?)",
                (job.job_id, job.repo, job.name, _json(job.labels), now, now),
            )
            return True
        if row["state"] in ("done", "cancelled"):
            # A job can be retried upstream; treat a fresh observation as queued.
            self.db.execute(
                "UPDATE jobs SET state='queued', updated_at=? WHERE job_id=?", (now, job.job_id)
            )
        return False

    def queued(self) -> list[Job]:
        rows = self.db.execute(
            "SELECT job_id, repo, name, labels FROM jobs WHERE state='queued' ORDER BY first_seen"
        ).fetchall()
        return [Job(r["job_id"], r["repo"], r["name"], tuple(_unjson(r["labels"]))) for r in rows]

    def set_state(self, job_id: str, state: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.db.execute("UPDATE jobs SET state=?, updated_at=? WHERE job_id=?", (state, now, job_id))

    def state_of(self, job_id: str) -> str | None:
        row = self.db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row["state"] if row else None

    def attempts(self, job_id: str) -> int:
        row = self.db.execute("SELECT attempts FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row["attempts"] if row else 0

    def bump_attempts(self, job_id: str) -> int:
        self.db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE job_id=?", (job_id,))
        return self.attempts(job_id)

    # ── leases ──────────────────────────────────────────────────────────────
    def next_fencing_token(self, job_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(fencing_token), 0) AS m FROM leases WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["m"]) + 1

    def grant(self, lease: Lease) -> None:
        self.db.execute(
            "INSERT INTO leases (lease_id, job_id, runner_name, fencing_token, state, granted_at)"
            " VALUES (?,?,?,?,'active',?)",
            (lease.lease_id, lease.job_id, lease.runner_name, lease.fencing_token, lease.granted_at),
        )
        if lease.job_id is not None:
            self.db.execute("UPDATE jobs SET state='running' WHERE job_id=?", (lease.job_id,))

    def assign_prewarm(self, lease_id: str, job_id: str, now: float | None = None) -> None:
        """Turn an idle prewarm lease into the lease for a specific job.

        The containers are interchangeable, so bookkeeping can point an existing
        warm runner at the job it is about to take rather than starting another.
        """
        now = now if now is not None else time.time()
        token = self.next_fencing_token(job_id)
        self.db.execute(
            "UPDATE leases SET job_id=?, fencing_token=?, granted_at=? WHERE lease_id=?",
            (job_id, token, now, lease_id),
        )
        self.db.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job_id,))

    def active_leases(self) -> list[Lease]:
        rows = self.db.execute(
            "SELECT lease_id, job_id, runner_name, fencing_token, state, granted_at"
            " FROM leases WHERE state='active'"
        ).fetchall()
        return [
            Lease(r["lease_id"], r["job_id"], r["runner_name"], r["fencing_token"],
                  r["state"], r["granted_at"])
            for r in rows
        ]

    def end_lease(self, lease_id: str, state: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.db.execute(
            "UPDATE leases SET state=?, ended_at=? WHERE lease_id=?", (state, now, lease_id)
        )

    def lease_count(self, job_id: str) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM leases WHERE job_id=?", (job_id,)).fetchone()
        return int(row["n"])


def _json(values: tuple[str, ...]) -> str:
    import json
    return json.dumps(list(values))


def _unjson(text: str) -> list[str]:
    import json
    return list(json.loads(text))
