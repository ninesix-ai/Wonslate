# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Static guards for ci/hooks/pre-commit.

The hook is the only gate that runs before a commit exists, and its test commands are
piped into `tail` to keep output short - a shape that swallows the exit status of the
tests unless `set -o pipefail` is active. That is not hypothetical: measured on
2026-09-27, a deliberately failing xUnit case committed cleanly through the hook.

These checks pin the properties that make the hook an actual gate rather than a printout.

Run:  python tests/test_pre_commit_hook.py
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = ROOT / "ci" / "hooks" / "pre-commit"

# Directories whose changes must trigger a regression check. The .NET ones are here
# because a project rename silently broke the pattern once already.
MUST_BE_MATCHED = ["Wonslate.UI", "Wonslate.UI.Tests", "translator-engine"]


def plain(text):
    """Strip everything that is not an alphanumeric so regex syntax cannot hide a name."""
    return re.sub(r"[^0-9A-Za-z]", "", text)


class PreCommitHookGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = HOOK.read_bytes()
        cls.text = cls.raw.decode("utf-8")
        cls.code = "\n".join(
            line for line in cls.text.splitlines() if not line.lstrip().startswith("#")
        )

    def test_pipefail_is_set_so_piped_tests_can_block(self):
        self.assertIn("set -o pipefail", self.code)

    def test_fail_fast_is_still_on(self):
        self.assertRegex(self.code, r"set\s+-e")

    def test_the_piped_test_commands_still_exist(self):
        # If the pipes are ever removed the pipefail guard becomes decorative; keep the
        # two facts tied together instead of letting the assertion rot silently.
        self.assertIn("| tail", self.code)
        self.assertRegex(self.code, r"dotnet\s+test")
        self.assertRegex(self.code, r"cargo\s+test")

    def test_every_relevant_project_directory_is_matched_by_the_hook(self):
        haystack = plain(self.code)
        for name in MUST_BE_MATCHED:
            self.assertTrue((ROOT / name).is_dir(), f"{name}/ no longer exists")
            self.assertIn(plain(name), haystack,
                          f"{name} is not referenced by the hook's staged-path patterns")

    def test_hook_is_bash_with_lf_endings(self):
        self.assertTrue(self.raw.startswith(b"#!/usr/bin/env bash"))
        self.assertNotIn(b"\r\n", self.raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
