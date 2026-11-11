#!/usr/bin/env python3
"""Tests for the metrics renderer."""
import tempfile
import unittest
from pathlib import Path

import metrics
from ledger import Job, Lease, Ledger


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.dir.name) / "ledger.db")
        self.stats = metrics.PassStats()
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.ledger.close()
        self.dir.cleanup()

    def render(self, live=None):
        return metrics.render(metrics.collect(self.ledger, self.stats, running=live, now=self.now))

    def test_counts_jobs_by_state(self):
        self.ledger.observe(Job("1", "o/r", "build", ("l",)), now=self.now - 100)
        self.ledger.observe(Job("2", "o/r", "test", ("l",)), now=self.now - 50)
        self.ledger.grant(Lease("a", "1", "runlet-1-1", 1, "active", self.now - 40))
        self.ledger.set_state("2", "done", now=self.now)

        text = self.render(live={"runlet-1-1"})
        self.assertIn('runlet_jobs{state="running"} 1', text)
        self.assertIn('runlet_jobs{state="done"} 1', text)
        self.assertIn('runlet_jobs{state="queued"} 0', text)

    def test_queued_wait_is_reported_per_job(self):
        self.ledger.observe(Job("1", "o/r", "deploy", ("l",)), now=self.now - 45)
        text = self.render()
        self.assertIn('runlet_job_wait_seconds{repo="o/r",job="deploy"} 45.0', text)
        self.assertIn("runlet_queued_oldest_seconds 45.0", text)

    def test_running_job_reports_elapsed_and_runner(self):
        self.ledger.observe(Job("1", "o/r", "build", ("l",)), now=self.now - 200)
        self.ledger.grant(Lease("a", "1", "runlet-1-2", 2, "active", self.now - 72))
        text = self.render(live={"runlet-1-2"})
        self.assertIn('runlet_job_running_seconds{repo="o/r",job="build",runner="runlet-1-2"} 72.0', text)
        self.assertIn('runlet_runners{kind="job"} 1', text)

    def test_missing_container_is_a_metric(self):
        self.ledger.observe(Job("1", "o/r", "build", ("l",)), now=self.now - 200)
        self.ledger.grant(Lease("a", "1", "runlet-1-1", 1, "active", self.now - 90))
        self.assertIn("runlet_runner_missing 1", self.render(live=set()))

    def test_warm_runners_are_counted_separately(self):
        self.ledger.grant(Lease("w", None, "runlet-warm-abc", 0, "active", self.now - 5))
        text = self.render(live={"runlet-warm-abc"})
        self.assertIn('runlet_runners{kind="warm"} 1', text)
        self.assertIn('runlet_runners{kind="job"} 0', text)

    def test_reconcile_counters_come_from_pass_stats(self):
        self.stats.record(0.042)
        self.stats.record(0.010, error="boom")
        text = self.render()
        self.assertIn("runlet_reconcile_total 2", text)
        self.assertIn("runlet_reconcile_failures_total 1", text)
        self.assertIn("runlet_reconcile_duration_seconds 0.0100", text)

    def test_every_family_has_help_and_type(self):
        text = self.render()
        for line in text.splitlines():
            if line.startswith("# HELP"):
                name = line.split()[2]
                self.assertIn(f"# TYPE {name} ", text, f"{name} has no TYPE line")

    def test_labels_with_quotes_and_backslashes_are_escaped(self):
        self.ledger.observe(Job('1', 'o/"r"', 'a\\b', ("l",)), now=self.now - 10)
        text = self.render()
        self.assertIn('runlet_job_wait_seconds{repo="o/\\"r\\"",job="a\\\\b"}', text)


if __name__ == "__main__":
    unittest.main()
