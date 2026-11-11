#!/usr/bin/env python3
"""Tests for the scheduler and ledger.

The source and runtime are faked, so these run in milliseconds and assert
behavior rather than plumbing: capacity limits, retry with fencing, completion
versus retry after a container exits, and restart safety.
"""
import tempfile
import unittest
from pathlib import Path

from ledger import Job, Ledger
from scheduler import Policy, Scheduler


class FakeSource:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])

    def queued(self):
        return list(self.jobs)

    def remove(self, job_id):
        self.jobs = [job for job in self.jobs if job.job_id != job_id]


class FakeRuntime:
    def __init__(self):
        self.live = {}
        self.started = []

    def running(self):
        return list(self.live)

    def start(self, runner_name, labels, fencing_token, repo="o/r"):
        self.started.append((runner_name, tuple(labels), fencing_token))
        self.live[runner_name] = labels
        return True

    def exit(self, runner_name):
        self.live.pop(runner_name, None)


def job(job_id, labels=("runlet-runner",)):
    return Job(job_id=job_id, repo="o/r", name=f"job-{job_id}", labels=tuple(labels))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.dir.name) / "ledger.db")
        self.runtime = FakeRuntime()
        self.source = FakeSource()

    def tearDown(self):
        self.ledger.close()
        self.dir.cleanup()

    def make(self, **policy):
        return Scheduler(self.ledger, self.source, self.runtime, Policy(**policy))

    def test_new_job_gets_exactly_one_runner(self):
        self.source.jobs = [job("a")]
        report = self.make().reconcile()
        self.assertEqual(len(report.granted), 1)
        self.assertEqual(len(self.runtime.started), 1)
        self.assertEqual(self.ledger.state_of("a"), "running")

    def test_capacity_is_enforced(self):
        self.source.jobs = [job(str(i)) for i in range(5)]
        report = self.make(max_runners=2).reconcile()
        self.assertEqual(len(report.granted), 2)
        self.assertEqual(len(self.runtime.started), 2)

    def test_job_is_dispatched_only_once(self):
        self.source.jobs = [job("a")]
        scheduler = self.make()
        scheduler.reconcile()
        second = scheduler.reconcile()
        self.assertEqual(second.granted, [])

    def test_container_exit_while_job_still_queued_retries_with_higher_fence(self):
        self.source.jobs = [job("a")]
        scheduler = self.make(grace_seconds=0)
        scheduler.reconcile()
        first_runner, _, first_token = self.runtime.started[0]

        self.runtime.exit(first_runner)
        report = scheduler.reconcile()

        self.assertIn("a", report.retried)
        # The retry happens in the same pass, so the job is dispatched again
        # rather than sitting queued until the next one.
        self.assertIn("a", report.granted)
        second_runner, _, second_token = [s for s in self.runtime.started if s[0] != first_runner][-1]
        self.assertGreater(second_token, first_token)
        self.assertNotEqual(second_runner, first_runner)

    def test_container_exit_after_job_left_the_queue_marks_done(self):
        self.source.jobs = [job("a")]
        scheduler = self.make(grace_seconds=0)
        scheduler.reconcile()
        runner, _, _ = self.runtime.started[0]

        # The runner took the job, so it is no longer queued upstream.
        self.source.remove("a")
        self.runtime.exit(runner)

        report = scheduler.reconcile()
        self.assertIn("a", report.completed)
        self.assertEqual(self.ledger.state_of("a"), "done")

    def test_retries_stop_at_the_attempt_limit(self):
        self.source.jobs = [job("a")]
        scheduler = self.make(grace_seconds=0, max_attempts=2)
        for _ in range(4):
            scheduler.reconcile()
            for runner in list(self.runtime.live):
                self.runtime.exit(runner)
        self.assertEqual(self.ledger.state_of("a"), "cancelled")

    def test_lost_container_before_grace_is_not_counted_as_failure(self):
        self.source.jobs = [job("a")]
        scheduler = self.make(grace_seconds=90)
        scheduler.reconcile()
        runner, _, _ = self.runtime.started[0]
        self.runtime.exit(runner)

        report = scheduler.reconcile()
        self.assertEqual(report.expired, [])
        self.assertEqual(report.retried, [])

    def test_restart_resumes_from_the_ledger(self):
        self.source.jobs = [job("a")]
        first = self.make()
        first.reconcile()
        self.ledger.close()

        # New process, same state file.
        self.ledger = Ledger(Path(self.dir.name) / "ledger.db")
        second = self.make()
        report = second.reconcile()
        self.assertEqual(report.granted, [], "an already leased job must not be dispatched twice")

    def test_warm_pool_starts_runners_without_jobs(self):
        report = self.make(min_warm=2, warm_labels=("runlet-runner",), warm_repo="o/r").reconcile()
        self.assertEqual(len(report.granted), 2)
        self.assertTrue(all(labels == ("runlet-runner",) for _, labels, _ in self.runtime.started))

    def test_job_uses_a_warm_runner_instead_of_starting_another(self):
        scheduler = self.make(min_warm=1, warm_labels=("runlet-runner",), warm_repo="o/r", max_runners=4)
        scheduler.reconcile()
        started_before = len(self.runtime.started)

        self.source.jobs = [job("a")]
        report = scheduler.reconcile()

        # One grant is the warm lease now serving the job; the other is the pool
        # refilling behind it.
        self.assertEqual(len([g for g in report.granted if g != "prewarm"]), 1)
        self.assertEqual(self.ledger.state_of("a"), "running")
        # The warm runner took the job, and the pool refilled behind it. No
        # job-specific runner was started.
        new_names = [name for name, _, _ in self.runtime.started[started_before:]]
        self.assertTrue(all(name.startswith("runlet-warm-") for name in new_names), new_names)


if __name__ == "__main__":
    unittest.main()
