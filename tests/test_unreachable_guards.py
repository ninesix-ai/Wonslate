# -*- coding: utf-8 -*-
"""Run script/check_unreachable_guards.py, and check that it checked something.

D65 was a validator that existed and was never executed; registering the gate in CI's list is
not enough, because the only copy of the truth then lives on a machine nobody opens until it is
red. So the suite that names the gate also runs it, and the gate in turn has to prove it opened
files rather than matched nothing.
"""
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import check_unreachable_guards as gate  # noqa: E402


class GateRunsTests(unittest.TestCase):
    def run_gate(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "script" / "check_unreachable_guards.py")]
                              + list(extra), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=str(ROOT))

    def test_the_gate_passes_on_this_repository(self):
        result = self.run_gate()
        self.assertEqual(0, result.returncode,
                         "check_unreachable_guards.py says:\n%s%s" % (result.stdout, result.stderr))

    def test_the_gate_self_test_fires_on_every_seed(self):
        result = self.run_gate("--self-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # Match "N/N" instead of the current count: hard-coding 8/8 would make the next seed
        # added fail this test for the wrong reason.
        self.assertRegex(result.stdout, r"(\d+)/(\1) seeded violations detected",
                         "the self-test did not report every seed firing: %s" % result.stdout)

    def test_the_gate_actually_audited_something(self):
        # "0 violations" from zero files looked at is indistinguishable from a clean repository.
        result = self.run_gate()
        self.assertRegex(result.stdout, r"(\d\d|\d{3,}) file\(s\) audited",
                         "the gate did not say it opened files: %s" % result.stdout)
        self.assertIn("0 violation(s)", result.stdout)
        self.assertIn("0 unparseable", result.stdout)

    def test_the_file_list_comes_from_git_not_from_a_roster(self):
        """The audited set must be exactly the tracked Python files.

        A hand-kept list drifts the way D65's validator did: silently, and in the direction of
        checking less.
        """
        tracked = set(gate.tracked_python())
        violations, broken, audited = gate.audit()
        self.assertEqual(len(broken) + audited, len(tracked),
                         "audited %d + unparseable %d != %d tracked files"
                         % (audited, len(broken), len(tracked)))

    def test_seeded_shapes_carry_both_directions(self):
        """The corpus itself must not be one-sided.

        Every seed that should fire needs a twin that must stay quiet, or the gate could report
        its own seeds while flagging ordinary code everywhere else -- the mistake that made the
        first version of the D73 line ban cry wolf over five docstrings.
        """
        for name, finder in (("SEED_TAKEN_PORT", gate.find_ordered_guards),
                             ("SEED_DEAD_CODE", gate.find_unreachable)):
            with self.subTest(name):
                text = getattr(gate, name)
                self.assertTrue(finder(gate.ast.parse(text)),
                                "%s no longer trips its own rule" % name)
        for name, finder in (("SEED_CORRECT_ORDER", gate.find_ordered_guards),
                             ("SEED_LEGITIMATE_EARLY_RETURN", gate.find_unreachable)):
            with self.subTest(name):
                text = getattr(gate, name)
                self.assertFalse(finder(gate.ast.parse(text)),
                                 "%s is being flagged, which means the rule overfits" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
