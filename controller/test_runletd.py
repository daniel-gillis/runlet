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


class ParseProfile(unittest.TestCase):
    def test_label_only_uses_the_default_image(self):
        self.assertEqual(runletd.parse_profile("runlet-runner", "default:1"),
                         ("runlet-runner", "default:1"))

    def test_label_equals_image_overrides(self):
        self.assertEqual(runletd.parse_profile("runlet-py=ghcr.io/x/py:2", "default:1"),
                         ("runlet-py", "ghcr.io/x/py:2"))

    def test_image_with_registry_and_digest_survives_parsing(self):
        spec = "runlet-py=ghcr.io/x/py@sha256:" + "a" * 64
        self.assertEqual(runletd.parse_profile(spec, "unused")[1], spec.split("=", 1)[1])

    def test_whitespace_is_trimmed(self):
        self.assertEqual(runletd.parse_profile("  runlet  =  img:1  ", "unused"),
                         ("runlet", "img:1"))


if __name__ == "__main__":
    unittest.main()
