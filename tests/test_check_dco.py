# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for script/check_dco.py.

DCO is enforced incrementally (the history before 2026-10-21 has no sign-offs and is exempt by
design). These tests pin the parts that make the gate trustworthy: every non-merge commit in the
range must carry a well-formed Signed-off-by, a malformed line does not count, merges are skipped,
and a bad range is an explicit refusal rather than a silent pass.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import check_dco  # noqa: E402


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo)] + list(args),
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, result.stderr))
    return result.stdout.strip()


def repo(tmp):
    path = pathlib.Path(tmp)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.com")
    git(path, "checkout", "-q", "-b", "main")
    # A signed baseline so `HEAD^` exists in every test; ranges below only ever look past it.
    git(path, "commit", "--allow-empty", "-q", "-m",
        "chore: init\n\nSigned-off-by: Test User <test@example.com>")
    return path


def commit(path, message):
    git(path, "commit", "--allow-empty", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


class DcoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = repo(self.tmp.name)

    def test_an_unsigned_commit_is_reported(self):
        sha = commit(self.repo, "feat: something")
        missing = check_dco.check(self.repo, sha)   # a single revision is a valid range
        self.assertEqual(1, len(missing))
        self.assertEqual(sha, missing[0].sha)

    def test_a_signed_commit_passes(self):
        commit(self.repo, "feat: something\n\nSigned-off-by: Test User <test@example.com>")
        self.assertEqual([], check_dco.check(self.repo, "HEAD^..HEAD"))

    def test_only_the_unsigned_commit_is_named_in_a_mixed_range(self):
        commit(self.repo, "chore: base\n\nSigned-off-by: Test User <test@example.com>")
        commit(self.repo, "feat: one\n\nSigned-off-by: Test User <test@example.com>")
        bad = commit(self.repo, "feat: two")
        missing = check_dco.check(self.repo, "HEAD~2..HEAD")
        self.assertEqual([bad], [m.sha for m in missing])

    def test_a_malformed_sign_off_does_not_count(self):
        for bad in ("Signed-off-by: Test User",
                    "Signed-off-by: <test@example.com>",
                    "Signed-off-by: Test User test@example.com",
                    "signed-off-by: Test User <test@example.com>"):
            commit(self.repo, "feat: bad\n\n" + bad)
            missing = check_dco.check(self.repo, "HEAD^..HEAD")
            self.assertEqual(1, len(missing), bad)

    def test_merge_commits_are_skipped(self):
        commit(self.repo, "feat: base\n\nSigned-off-by: Test User <test@example.com>")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "side")
        commit(self.repo, "feat: side\n\nSigned-off-by: Test User <test@example.com>")
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, "feat: main\n\nSigned-off-by: Test User <test@example.com>")
        git(self.repo, "merge", "--no-ff", "-m", "merge side (unsigned on purpose)", "side")
        self.assertEqual([], check_dco.check(self.repo, base + "..HEAD"))

    def test_a_bad_range_is_refused(self):
        with self.assertRaises(check_dco.DcoError):
            check_dco.check(self.repo, "does-not-exist..HEAD")
        self.assertEqual(2, check_dco.main(["--repo", str(self.repo),
                                            "--range", "nope..HEAD"]))

    def test_cli_reports_missing_and_passes_signed(self):
        commit(self.repo, "feat: first\n\nSigned-off-by: Test User <test@example.com>")
        self.assertEqual(0, check_dco.main(["--repo", str(self.repo),
                                            "--range", "HEAD^..HEAD"]))
        commit(self.repo, "feat: second")
        self.assertEqual(1, check_dco.main(["--repo", str(self.repo),
                                            "--range", "HEAD^..HEAD"]))

    def test_an_empty_range_passes_with_a_clear_message(self):
        commit(self.repo, "feat: only\n\nSigned-off-by: Test User <test@example.com>")
        self.assertEqual([], check_dco.check(self.repo, "HEAD..HEAD"))


if __name__ == "__main__":
    unittest.main()
