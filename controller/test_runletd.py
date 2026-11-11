#!/usr/bin/env python3
"""Tests for the runletd controller.

Deliberately no network and no docker: every function under test is pure, so the
suite runs in CI in milliseconds and can be trusted in a way the controller's
integration behaviour cannot.
"""
import unittest

import runletd


class LabelNames(unittest.TestCase):
    def test_accepts_strings_as_returned_by_the_jobs_endpoint(self):
        job = {"labels": ["self-hosted", "Linux", "X64"]}
        self.assertEqual(runletd.label_names(job), ["self-hosted", "Linux", "X64"])

    def test_accepts_objects_as_returned_by_the_runners_endpoint(self):
        job = {"labels": [{"name": "self-hosted"}, {"name": "runlet-runner"}]}
        self.assertEqual(runletd.label_names(job), ["self-hosted", "runlet-runner"])

    def test_missing_labels_is_empty_not_an_error(self):
        self.assertEqual(runletd.label_names({}), [])
        self.assertEqual(runletd.label_names({"labels": None}), [])

    def test_empty_names_are_dropped(self):
        job = {"labels": [{"name": "ok"}, {"name": ""}, "", None]}
        self.assertEqual(runletd.label_names(job), ["ok"])


class Profile(unittest.TestCase):
    def test_serves_only_when_its_label_is_present(self):
        profile = runletd.Profile("runlet-runner", "img:1")
        self.assertTrue(profile.serves({"labels": ["runlet-runner"]}))
        self.assertTrue(profile.serves({"labels": ["self-hosted", "runlet-runner"]}))
        self.assertFalse(profile.serves({"labels": ["self-hosted"]}))

    def test_a_job_with_no_labels_is_never_served(self):
        # Guessing here would mean starting a runner that can never match.
        profile = runletd.Profile("runlet-runner", "img:1")
        self.assertFalse(profile.serves({"labels": []}))

    def test_other_labels_do_not_prevent_a_match(self):
        profile = runletd.Profile("runlet-runner", "img:1")
        self.assertTrue(profile.serves({"labels": ["linux", "gpu", "runlet-runner"]}))

    def test_prefix_is_unique_per_label(self):
        self.assertNotEqual(runletd.Profile("a", "i").prefix, runletd.Profile("b", "i").prefix)


class ParseProfile(unittest.TestCase):
    def test_label_only_uses_the_default_image(self):
        profile = runletd.parse_profile("runlet-runner", "default:1")
        self.assertEqual(profile.label, "runlet-runner")
        self.assertEqual(profile.image, "default:1")

    def test_label_equals_image_overrides(self):
        profile = runletd.parse_profile("runlet-py=ghcr.io/x/py:2", "default:1")
        self.assertEqual(profile.label, "runlet-py")
        self.assertEqual(profile.image, "ghcr.io/x/py:2")

    def test_image_with_registry_and_digest_survives_parsing(self):
        spec = "runlet-py=ghcr.io/x/py@sha256:" + "a" * 64
        profile = runletd.parse_profile(spec, "unused")
        self.assertEqual(profile.image, spec.split("=", 1)[1])

    def test_whitespace_is_trimmed(self):
        profile = runletd.parse_profile("  runlet  =  img:1  ", "unused")
        self.assertEqual(profile.label, "runlet")
        self.assertEqual(profile.image, "img:1")


if __name__ == "__main__":
    unittest.main()
