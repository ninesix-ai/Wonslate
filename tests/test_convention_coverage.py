# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Run script/check_convention_coverage.py, and prove it refuses an unowned convention (D79).

The point of that gate is one sentence: *a convention nobody enforces decays silently*. It cannot be
trusted to hold that line unless someone plants the decay and watches it get caught -- so the main
case here edits CONTRIBUTING.md for real, adds a convention with no row, runs the gate, and requires
red. The edit is undone byte-for-byte in cleanup, and the undo is itself asserted: a test that
leaves a planted rule behind would teach the next reader that the gate is broken.

Everything else is the supporting cast: the grades must not drift upward on their own (a `planted`
row has to say what was planted), the pointers must point at files that exist (this round found a
docstring promising a test suite that never existed), and the gap count must be printed rather than
inferred from silence.
"""
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import check_convention_coverage as gate     # noqa: E402

SCRIPT = ROOT / "script" / "check_convention_coverage.py"


def run_gate(*args):
    return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT)] + list(args),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT))


class GateRunsTests(unittest.TestCase):
    def test_self_test_reports_every_seed_and_passes(self):
        proc = run_gate("--self-test")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        first = proc.stdout.splitlines()[0]
        fired, _slash, total = first.split(" ")[0].partition("/")
        self.assertEqual(int(fired), int(total), "some seeded judgement is wrong: %s" % first)
        self.assertGreaterEqual(int(total), 12, first)

    def test_the_real_table_is_clean(self):
        proc = run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout)

    def test_an_unregistered_convention_makes_the_gate_red(self):
        """Plant the exact decay this gate exists to catch, in the real file.

        The line has to land *inside* `## Conventions`: the first draft appended it to the end of
        the file, where the section walk has already stopped collecting, and the gate correctly
        reported nothing. A planted violation that the checker cannot see is a green test about
        nothing -- which is precisely the failure mode this suite is here to make impossible.
        """
        original = gate.CONTRIB.read_bytes()
        text = original.decode("utf-8")
        heading = "## Conventions\n"
        planted = ("- **A brand new rule invented for this test** -- nothing enforces it, "
                   "which is the point.\n")
        self.assertIn(heading, text, "CONTRIBUTING.md no longer opens its conventions section "
                                     "where this test expects: the seed would plant nowhere")
        try:
            gate.CONTRIB.write_bytes(text.replace(heading, heading + planted, 1).encode("utf-8"))
            proc = run_gate()
            self.assertEqual(1, proc.returncode, proc.stdout)
            self.assertIn("A brand new rule invented for this test", proc.stdout)
            self.assertIn("prose without an owner", proc.stdout)
        finally:
            gate.CONTRIB.write_bytes(original)
        # Asserting the undo, not assuming it: a leftover planted rule reads as a broken gate.
        self.assertEqual(original, gate.CONTRIB.read_bytes(),
                         "the planted convention was not removed byte-for-byte")
        self.assertEqual(0, run_gate().returncode, "the gate did not recover after cleanup")

    def test_every_row_names_a_file_that_exists(self):
        problems = gate.check_rows(gate.CONVENTIONS, lambda target: (ROOT / target).exists())
        self.assertEqual([], [p for p in problems if "does not exist" in p],
                         "a row points at a checker that is not there")
        # The assert above can never fail when the table is clean, so it proves nothing about the
        # judgement: two mutations of `check_rows` survived it during this round's self-test. The
        # constructed case below is what actually shows the rule bites.
        bites = gate.check_rows([dict(gate.GOOD_ROW, enforced_by="tests/no_such_suite.py")],
                                lambda target: False)
        self.assertTrue([p for p in bites if "does not exist" in p],
                        "check_rows accepted a pointer to a file that is not there")
        quiet = gate.check_rows([dict(gate.GOOD_ROW)], lambda target: True)
        self.assertEqual([], [p for p in quiet if "does not exist" in p],
                         "the rule fires on a pointer that is fine")

    def test_the_gap_row_is_printed_not_assumed(self):
        # `asserted` is the honest label for a convention nobody checks. It has to stay visible in
        # the counts, otherwise the table reads as if everything were enforced.
        proc = run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout)
        counts = [l for l in proc.stdout.splitlines() if l.startswith("counts:")][0]
        self.assertIn("asserted=1", counts, counts)
        asserted = [row for row in gate.CONVENTIONS if row["kind"] == "asserted"]
        self.assertEqual(1, len(asserted))
        self.assertIn("Conventional Commits", asserted[0]["source"])
        self.assertEqual("", asserted[0]["enforced_by"],
                         "an acknowledged gap must not pretend to name an enforcer")

    def test_grades_cannot_be_upgraded_by_wishful_thinking(self):
        for row in gate.CONVENTIONS:
            with self.subTest(source=row["source"]):
                if row["kind"] == "planted":
                    self.assertIn("plant", row["note"].lower(),
                                  "'planted' claims a violation was fed to the checker; the note "
                                  "has to say which")
                if row["kind"] == "asserted":
                    self.assertFalse(row["enforced_by"])
        # Constructed halves, for the same reason as above: sweeping the real table cannot show the
        # judgement works, and a mutation of this rule survived the sweep.
        inflated = gate.check_rows([dict(gate.GOOD_ROW, kind="planted", note="a checker exists")],
                                   lambda target: True)
        self.assertTrue([p for p in inflated if "never says what was planted" in p], inflated)
        tidy = gate.check_rows([dict(gate.GOOD_ROW, kind="planted",
                                     note="planted a header-less file")], lambda target: True)
        self.assertEqual([], [p for p in tidy if "never says" in p], tidy)
        contradictory = gate.check_rows([dict(gate.GOOD_ROW, kind="asserted",
                                              note="nothing checks this")], lambda target: True)
        self.assertTrue([p for p in contradictory if "graded 'asserted'" in p], contradictory)

    def test_json_output_is_parseable_and_carries_the_verdict(self):
        # The gate is itself a structured artefact deliverable, so its own output must parse.
        out = ROOT / "convention-coverage.demo.json"
        self.addCleanup(lambda: out.unlink() if out.exists() else None)
        proc = run_gate("--json", out.name)
        self.assertEqual(0, proc.returncode, proc.stdout)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([], payload["problems"])
        self.assertEqual(payload["counts"]["declared"], len(gate.declared_conventions()))
        self.assertNotIn(b"\r\n", out.read_bytes(), "committed artefacts stay LF")

    def test_declared_conventions_reads_the_real_sections(self):
        declared = gate.declared_conventions()
        self.assertGreaterEqual(len(declared), 10, declared)
        self.assertFalse([d for d in declared if d.startswith("!UNLED:")], declared)
        # Names are the join key, so they have to appear verbatim in the file.
        text = gate.CONTRIB.read_text(encoding="utf-8")
        for name in declared:
            self.assertIn("**%s**" % name, text, "%s was synthesised, not read" % name)


class RegistrationTests(unittest.TestCase):
    NAME = "test_convention_coverage.py"

    def test_the_suite_runs_locally_and_in_ci_alike(self):
        build = (ROOT / "script" / "build.py").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(self.NAME, build)
        self.assertIn(self.NAME, ci)

    def test_the_agents_table_points_at_the_gate(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("script/check_convention_coverage.py", agents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
