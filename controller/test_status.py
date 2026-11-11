#!/usr/bin/env python3
"""Tests for the status renderer.

The renderer is pure given a ledger and a clock, so these assert on the text a
person would actually read.
"""
import tempfile
import unittest
from pathlib import Path

import status
from ledger import Job, Lease, Ledger


class DurationTest(unittest.TestCase):
    def test_formats_seconds_minutes_and_hours(self):
        self.assertEqual(status.duration(0), "0s")
        self.assertEqual(status.duration(8), "8s")
        self.assertEqual(status.duration(59), "59s")
        self.assertEqual(status.duration(192), "3m12s")
        self.assertEqual(status.duration(3725), "1h02m")

    def test_negative_is_clamped(self):
        self.assertEqual(status.duration(-5), "0s")


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.dir.name) / "ledger.db")
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.ledger.close()
        self.dir.cleanup()

    def add_running(self, job_id="1", name="build", repo="o/r", age=72.0, token=1):
        self.ledger.observe(Job(job_id, repo, name, ("runlet-runner",)), now=self.now - age - 10)
        self.ledger.grant(Lease("lease-1", job_id, f"runlet-{job_id}-{token}", token,
                                "active", self.now - age))

    def add_queued(self, job_id="2", name="test", repo="o/r", waited=45.0):
        self.ledger.observe(Job(job_id, repo, name, ("runlet-runner",)), now=self.now - waited)

    def render(self, live=None, max_runners=None):
        original = status.live_containers
        status.live_containers = lambda prefix="runlet-": set(live or [])
        try:
            state = status.collect(self.ledger, now=self.now)
            return status.render(state, max_runners)
        finally:
            status.live_containers = original

    def test_running_job_shows_name_repo_and_elapsed(self):
        self.add_running(name="integration tests", age=192.0)
        text = self.render(live={"runlet-1-1"})
        self.assertIn("RUNNING 1", text)
        self.assertIn("integration tests", text)
        self.assertIn("o/r", text)
        self.assertIn("3m12s", text)

    def test_capacity_is_shown_when_known(self):
        self.add_running()
        self.assertIn("RUNNING 1 of 8", self.render(live={"runlet-1-1"}, max_runners=8))

    def test_missing_container_is_flagged(self):
        self.add_running()
        text = self.render(live=set())
        self.assertIn("container gone, will retry", text)

    def test_retry_attempt_is_visible(self):
        self.add_running(token=2)
        self.assertIn("attempt 2", self.render(live={"runlet-1-2"}))

    def test_queued_job_shows_wait_and_labels(self):
        self.add_queued(waited=45.0)
        text = self.render()
        self.assertIn("QUEUED 1", text)
        self.assertIn("test", text)
        self.assertIn("45s", text)
        self.assertIn("runlet-runner", text)

    def test_empty_state_says_nothing_rather_than_zero(self):
        text = self.render()
        self.assertIn("RUNNING nothing", text)
        self.assertIn("QUEUED nothing", text)

    def test_warm_runners_are_counted_separately(self):
        self.ledger.grant(Lease("w", None, "runlet-warm-abc", 0, "active", self.now - 5))
        text = self.render(live={"runlet-warm-abc"})
        self.assertIn("+1 warm", text)
        self.assertIn("waiting for a job", text)

    def test_short_summary_is_one_line(self):
        self.add_running()
        self.add_queued()
        state = status.collect(self.ledger, now=self.now)
        line = status.short(state)
        self.assertIn("1 running", line)
        self.assertIn("1 queued", line)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
